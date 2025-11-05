import os
import time
from http.client import HTTPException
from threading import Thread

from requests import RequestException

from core.tiktok_api import TikTokAPI
from utils.logger_manager import logger
from utils.video_management import VideoManagement
from upload.telegram import Telegram
from utils.custom_exceptions import LiveNotFound, UserLiveError, TikTokRecorderError
from utils.enums import Mode, Error, TimeOut, TikTokError


class TikTokRecorder:
    def __init__(
        self,
        url,
        user,
        room_id,
        mode,
        automatic_interval,
        cookies,
        proxy,
        output,
        duration,
        use_telegram,
        segment_time
    ):
        # Setup TikTok API client
        self.tiktok = TikTokAPI(proxy=proxy, cookies=cookies)

        # TikTok Data
        self.url = url
        self.user = user
        self.room_id = room_id

        # Tool Settings
        self.mode = mode
        self.automatic_interval = automatic_interval
        self.duration = duration
        self.output = output

        # Upload Settings
        self.use_telegram = use_telegram
        self.segment_time = segment_time

        # Check if the user's country is blacklisted
        self.check_country_blacklisted()

        # Retrieve sec_uid if the mode is FOLLOWERS
        if self.mode == Mode.FOLLOWERS:
            self.sec_uid = self.tiktok.get_sec_uid()
            if self.sec_uid is None:
                raise TikTokRecorderError("Failed to retrieve sec_uid.")

            logger.info("Followers mode activated\n")
        else:
            # Get live information based on the provided user data
            if self.url:
                self.user, self.room_id = self.tiktok.get_room_and_user_from_url(
                    self.url
                )

            if not self.user:
                self.user = self.tiktok.get_user_from_room_id(self.room_id)

            if not self.room_id:
                self.room_id = self.tiktok.get_room_id_from_user(self.user)

            logger.info(f"USERNAME: {self.user}" + ("\n" if not self.room_id else ""))
            if self.room_id:
                logger.info(
                    f"ROOM_ID:  {self.room_id}"
                    + ("\n" if not self.tiktok.is_room_alive(self.room_id) else "")
                )

        # If proxy is provided, set up the HTTP client without the proxy
        if proxy:
            self.tiktok = TikTokAPI(proxy=None, cookies=cookies)

    def run(self):
        """
        runs the program in the selected mode.

        If the mode is MANUAL, it checks if the user is currently live and
        if so, starts recording.

        If the mode is AUTOMATIC, it continuously checks if the user is live
        and if not, waits for the specified timeout before rechecking.
        If the user is live, it starts recording.

        if the mode is FOLLOWERS, it continuously checks the followers of
        the authenticated user. If any follower is live, it starts recording
        their live stream in a separate process.
        """
        if self.mode == Mode.MANUAL:
            self.manual_mode()

        elif self.mode == Mode.AUTOMATIC:
            self.automatic_mode()

        elif self.mode == Mode.FOLLOWERS:
            self.followers_mode()

    def manual_mode(self):
        if not self.tiktok.is_room_alive(self.room_id):
            raise UserLiveError(f"@{self.user}: {TikTokError.USER_NOT_CURRENTLY_LIVE}")

        self.start_recording(self.user, self.room_id)

    def automatic_mode(self):
        while True:
            try:
                self.room_id = self.tiktok.get_room_id_from_user(self.user)
                self.manual_mode()

            except UserLiveError as ex:
                logger.info(ex)
                logger.info(
                    f"Waiting {self.automatic_interval} minutes before recheck\n"
                )
                time.sleep(self.automatic_interval * TimeOut.ONE_MINUTE)

            except LiveNotFound as ex:
                logger.error(f"Live not found: {ex}")
                logger.info(
                    f"Waiting {self.automatic_interval} minutes before recheck\n"
                )
                time.sleep(self.automatic_interval * TimeOut.ONE_MINUTE)

            except ConnectionError:
                logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                time.sleep(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)

            except Exception as ex:
                logger.error(f"Unexpected error: {ex}\n")

    def followers_mode(self):
        active_recordings = {}  # follower -> Process

        while True:
            try:
                followers = self.tiktok.get_followers_list(self.sec_uid)

                for follower in followers:
                    if follower in active_recordings:
                        if not active_recordings[follower].is_alive():
                            logger.info(f"Recording of @{follower} finished.")
                            del active_recordings[follower]
                        else:
                            continue

                    try:
                        room_id = self.tiktok.get_room_id_from_user(follower)

                        if not room_id or not self.tiktok.is_room_alive(room_id):
                            # logger.info(f"@{follower} is not live. Skipping...")
                            continue

                        logger.info(f"@{follower} is live. Starting recording...")

                        thread = Thread(
                            target=self.start_recording,
                            args=(follower, room_id),
                            daemon=True,
                        )
                        thread.start()
                        active_recordings[follower] = thread

                        time.sleep(2.5)

                    except Exception as e:
                        logger.error(f"Error while processing @{follower}: {e}")
                        continue

                print()
                delay = self.automatic_interval * TimeOut.ONE_MINUTE
                logger.info(f"Waiting {delay} minutes for the next check...")
                time.sleep(delay)

            except UserLiveError as ex:
                logger.info(ex)
                logger.info(
                    f"Waiting {self.automatic_interval} minutes before recheck\n"
                )
                time.sleep(self.automatic_interval * TimeOut.ONE_MINUTE)

            except ConnectionError:
                logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                time.sleep(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)

            except Exception as ex:
                logger.error(f"Unexpected error: {ex}\n")

    def start_recording(self, user, room_id):
        """
        Start recording live
        """
        live_url = self.tiktok.get_live_url(room_id)
        if not live_url:
            raise LiveNotFound(TikTokError.RETRIEVE_LIVE_URL)

        current_date = time.strftime("%Y.%m.%d_%H-%M-%S", time.localtime())

        if isinstance(self.output, str) and self.output != "":
            if not (self.output.endswith("/") or self.output.endswith("\\")):
                if os.name == "nt":
                    self.output = self.output + "\\"
                else:
                    self.output = self.output + "/"

        output = f"{self.output if self.output else ''}TK_{user}_{current_date}_flv.mp4"

        if self.duration:
            logger.info(f"Started recording for {self.duration} seconds ")
        else:
            logger.info("Started recording...")

        buffer_size = 512 * 1024  # 512 KB buffer
        buffer = bytearray()

        logger.info("[PRESS CTRL + C ONCE TO STOP]")
        with open(output, "wb") as out_file:
            stop_recording = False
            while not stop_recording:
                try:
                    if not self.tiktok.is_room_alive(room_id):
                        logger.info("User is no longer live. Stopping recording.")
                        break

                    start_time = time.time()
                    for chunk in self.tiktok.download_live_stream(live_url):
                        buffer.extend(chunk)
                        if len(buffer) >= buffer_size:
                            out_file.write(buffer)
                            buffer.clear()

                        elapsed_time = time.time() - start_time
                        if self.duration and elapsed_time >= self.duration:
                            stop_recording = True
                            break

                except ConnectionError:
                    if self.mode == Mode.AUTOMATIC:
                        logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                        time.sleep(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)

                except (RequestException, HTTPException):
                    time.sleep(2)

                except KeyboardInterrupt:
                    logger.info("Recording stopped by user.")
                    stop_recording = True

                except Exception as ex:
                    logger.error(f"Unexpected error: {ex}\n")
                    stop_recording = True

                finally:
                    if buffer:
                        out_file.write(buffer)
                        buffer.clear()
                    out_file.flush()

        logger.info(f"Recording finished: {output}\n")
        VideoManagement.convert_flv_to_mp4(output)

        if self.use_telegram:
            Telegram().upload(output.replace("_flv.mp4", ".mp4"))

    def start_recording(self, user, room_id):
        """
        Start recording live.
        Điều phối việc ghi hình, chọn giữa ghi một file hoặc ghi phân đoạn.
        """
        live_url = self.tiktok.get_live_url(room_id)
        if not live_url:
            raise LiveNotFound(TikTokError.RETRIEVE_LIVE_URL)

        current_date = time.strftime("%Y.%m.%d_%H-%M-%S", time.localtime())

        # Tạo thư mục đầu ra
        if isinstance(self.output, str) and self.output != "":
            if not (self.output.endswith("/") or self.output.endswith("\\")):
                if os.name == "nt":
                    self.output = self.output + "\\"
                else:
                    self.output = self.output + "/"

        base_output_path = self.output if self.output else ''
        base_filename = f"TK_{user}_{current_date}"

        # Quyết định logic ghi hình
        if self.segment_time and self.segment_time > 0:
            logger.info(f"Started recording in segments of {self.segment_time} seconds...")
            self._record_segmented(user, room_id, live_url, base_output_path, base_filename)
        else:
            # Logic ghi file đơn (như ban đầu)
            output_flv = f"{base_output_path}{base_filename}_flv.mp4"

            if self.duration:
                logger.info(f"Started recording for {self.duration} seconds ")
            else:
                logger.info("Started recording...")

            self._record_single_file(room_id, live_url, output_flv)

            # Xử lý sau khi ghi file đơn
            logger.info(f"Recording finished: {output_flv}\n")
            VideoManagement.convert_flv_to_mp4(output_flv)

            if self.use_telegram:
                Telegram().upload(output_flv.replace("_flv.mp4", ".mp4"))

    def _record_single_file(self, room_id, live_url, output_flv):
        """
        Ghi toàn bộ stream vào một file duy nhất.
        Đây là logic gốc từ hàm start_recording của bạn.
        """
        buffer_size = 512 * 1024  # 512 KB buffer
        buffer = bytearray()

        logger.info("[PRESS CTRL + C ONCE TO STOP]")
        with open(output_flv, "wb") as out_file:
            stop_recording = False
            while not stop_recording:
                try:
                    if not self.tiktok.is_room_alive(room_id):
                        logger.info("User is no longer live. Stopping recording.")
                        break

                    start_time = time.time()
                    for chunk in self.tiktok.download_live_stream(live_url):
                        buffer.extend(chunk)
                        if len(buffer) >= buffer_size:
                            out_file.write(buffer)
                            buffer.clear()

                        elapsed_time = time.time() - start_time
                        if self.duration and elapsed_time >= self.duration:
                            logger.info(f"Target duration of {self.duration}s reached.")
                            stop_recording = True
                            break

                except ConnectionError:
                    if self.mode == Mode.AUTOMATIC:
                        logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                        time.sleep(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)
                    # Trong chế độ MANUAL, lỗi kết nối sẽ khiến vòng lặp thử lại

                except (RequestException, HTTPException):
                    time.sleep(2)

                except KeyboardInterrupt:
                    logger.info("Recording stopped by user.")
                    stop_recording = True

                except Exception as ex:
                    logger.error(f"Unexpected error: {ex}\n")
                    stop_recording = True

                finally:
                    if buffer:
                        out_file.write(buffer)
                        buffer.clear()
                    out_file.flush()

    def _record_segmented(self, user, room_id, live_url, base_output_path, base_filename):
        """
        Ghi stream thành nhiều file, mỗi file có thời lượng self.segment_time.
        - An toàn hơn: luôn flush & close file trước khi tạo file mới.
        - Không bị ghi đè file: dùng file tạm (.part) rồi rename sang .mp4 sau khi xong.
        - Xử lý reconnect & KeyboardInterrupt hợp lý.
        """
        segment_number = 1
        stop_recording = False
        buffer_size = 512 * 1024  # 512 KB buffer
        buffer = bytearray()
        total_start_time = time.time()

        logger.info("[PRESS CTRL + C ONCE TO STOP]")

        while not stop_recording:
            # Đặt tên file tạm (.part) để tránh mất dữ liệu nếu crash giữa chừng
            temp_output = f"{base_output_path}{segment_number}_{base_filename}_flv.part"
            final_output = temp_output.replace(".part", ".mp4")

            segment_start_time = time.time()
            logger.info(f"Starting new segment: {final_output}")

            try:
                with open(temp_output, "wb") as out_file:
                    while True:
                        try:
                            if not self.tiktok.is_room_alive(room_id):
                                logger.info("User is no longer live. Stopping recording.")
                                stop_recording = True
                                break

                            for chunk in self.tiktok.download_live_stream(live_url):
                                buffer.extend(chunk)
                                if len(buffer) >= buffer_size:
                                    out_file.write(buffer)
                                    buffer.clear()

                                current_time = time.time()

                                # Kiểm tra thời gian segment
                                if (current_time - segment_start_time) >= self.segment_time:
                                    logger.info(f"Segment {segment_number} duration reached ({self.segment_time}s).")
                                    break

                                # Kiểm tra tổng thời lượng (nếu có self.duration)
                                if self.duration and (current_time - total_start_time) >= self.duration:
                                    logger.info(f"Total duration {self.duration}s reached. Stopping recording.")
                                    stop_recording = True
                                    break

                            # Ghi nốt buffer còn lại
                            if buffer:
                                out_file.write(buffer)
                                buffer.clear()

                            # Nếu kết thúc segment hoặc hết thời lượng thì thoát khỏi vòng lặp
                            if (time.time() - segment_start_time) >= self.segment_time or stop_recording:
                                break

                        except (ConnectionError, RequestException, HTTPException):
                            logger.warning("Connection issue. Retrying in 2s...")
                            time.sleep(2)
                            continue

                        except KeyboardInterrupt:
                            logger.info("Recording stopped by user.")
                            stop_recording = True
                            break

                        except Exception as ex:
                            logger.error(f"Unexpected error while recording segment {segment_number}: {ex}")
                            stop_recording = True
                            break

            finally:
                # Đảm bảo file luôn được đóng đúng cách
                if os.path.exists(temp_output):
                    # Rename sang file mp4 sau khi hoàn tất segment
                    os.rename(temp_output, final_output)
                    logger.info(f"Segment {segment_number} saved: {final_output}")

                    # Convert và upload
                    VideoManagement.convert_flv_to_mp4(final_output)
                    if self.use_telegram:
                        Telegram().upload(final_output.replace("_flv.mp4", ".mp4"))

            if stop_recording:
                break

            # Sang segment mới
            segment_number += 1
            logger.info(f"Waiting for next segment...\n")

        logger.info("Segmented recording finished.\n")


    def check_country_blacklisted(self):
        is_blacklisted = self.tiktok.is_country_blacklisted()
        if not is_blacklisted:
            return False

        if self.room_id is None:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED)

        if self.mode == Mode.AUTOMATIC:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED_AUTO_MODE)

        elif self.mode == Mode.FOLLOWERS:
            raise TikTokRecorderError(TikTokError.COUNTRY_BLACKLISTED_FOLLOWERS_MODE)

        return is_blacklisted
