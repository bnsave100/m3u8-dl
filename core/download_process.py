from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, Future, wait
from multiprocessing import current_process, JoinableQueue
from .common.constants import IP, PORT, HEADER_SIZE
from typing import List, Dict, Optional
from .weblib.fetch import fetch_data
from .common.base import Client
from traceback import print_exc
from random import shuffle
from queue import Queue
from time import time
import requests
import pickle
import sys
import os


def download_process(links, total_links, session, http2, max_retries,
                     convert, file_link_maps, path_prefix, debug) -> None:
    print(f"Starting Download process {current_process().name}")
    start_time = time()
    try:
        download_manager = DownloadProcess(links, total_links, session, http2,
                                           max_retries, convert, debug)

        start_processes(download_manager, file_link_maps, path_prefix)
        try:
            client = Client(IP, PORT)
            client.send_data(f"{'STOP_QUEUE':<{HEADER_SIZE}}{download_manager.get_total_downloaded_links_count()}")
        except (ConnectionRefusedError, ConnectionResetError):
            print_exc()

    except (KeyboardInterrupt, Exception):
        print_exc()

    print(f"Download took {time() - start_time} seconds")
    print(f"Stopped Download process {current_process().name}")


class DownloadProcess:
    def __init__(self, links: List[str], total_links: int, session: requests.Session,
                 http2: bool = False, max_retries: int = 5,
                 convert: bool = True, debug: bool = False):
        self.__session: requests.Session = session
        self.__total_links: int = total_links
        self.__links: List[str] = links
        self.max_retries: int = max_retries
        self.http2: bool = http2
        self.convert = convert
        self.__sent = 0
        self.__process_num = len(os.sched_getaffinity(os.getpid()))
        self.__thread_num = (total_links - self.__sent) // self.__process_num
        self.debug = debug
        self.done_retries = 0
        self.error_links = []

    def get_thread_num(self) -> int:
        return self.__thread_num

    def get_process_num(self) -> int:
        return self.__process_num

    def set_thread_num(self, val: int) -> None:
        self.__thread_num = val

    def get_download_links(self) -> List[str]:
        return self.__links

    def get_total_links(self) -> int:
        return self.__total_links

    def get_session(self) -> requests.Session:
        return self.__session

    def get_total_downloaded_links_count(self) -> int:
        return self.__sent

    def set_total_downloaded_links_count(self, val: int) -> None:
        self.__sent = val


def start_processes(download_manager: DownloadProcess, file_link_maps: Dict[str, str], path_prefix: str) -> None:
    process_num: int = download_manager.get_process_num()
    if download_manager.debug:
        print(f"starting {process_num} processes for {len(download_manager.get_download_links())} links")

    with ProcessPoolExecutor(max_workers=process_num) as process_pool_executor:
        try:
            process_pool_executor_handler(process_pool_executor, download_manager, file_link_maps, path_prefix)
        except (KeyboardInterrupt, Exception):
            sys.exit()


def process_pool_executor_handler(executor: ProcessPoolExecutor, manager: DownloadProcess,
                                  file_maps: Dict[str, str], directory: str) -> None:

    done_queue = JoinableQueue()

    def update_hook(future: Future):
        temp = future.result()
        if temp:
            for failed_links in temp:
                done_queue.put(failed_links)

    while manager.done_retries != manager.max_retries:
        print(f"Starting download {manager.get_total_links() - manager.get_total_downloaded_links_count()} links left")
        available_cpus = list(os.sched_getaffinity(os.getpid()))
        print(f"available cpu's {available_cpus}, initializing {5 * manager.get_process_num()}"
              f" threads with {manager.get_thread_num()} links per "
              f"process")

        if len(manager.error_links):
            shuffle(manager.error_links)
            download_links = manager.error_links.copy()
            manager.error_links = []
        else:
            download_links = manager.get_download_links().copy()
            shuffle(download_links)

        process_futures: List[Future] = []

        start = 0
        for temp_num in range(len(download_links)):
            end = start + manager.get_thread_num()
            if end > len(download_links):
                end = len(download_links)
            cpu_num = available_cpus[temp_num % len(available_cpus)]

            if manager.debug:
                print(f"running on cpu {cpu_num} from available cpus {available_cpus}")

            process_futures.append(executor.submit(start_threads, download_links[start:end],
                                                   file_maps, manager.get_session(), directory,
                                                   manager.http2, manager.debug, cpu_num))
            process_futures[-1].add_done_callback(update_hook)
            start = end
            if end >= len(download_links):
                break

        wait(process_futures)

        while not done_queue.empty():
            link = done_queue.get()
            done_queue.task_done()
            manager.error_links.append(link)

        manager.set_total_downloaded_links_count(manager.get_total_links() - len(manager.error_links))

        if manager.debug:
            print(f"Total downloaded links {manager.get_total_downloaded_links_count()}")
            print(f"Error links generated {len(manager.error_links)}")

        if len(manager.error_links):
            manager.set_thread_num((manager.get_total_links()
                                    - manager.get_total_downloaded_links_count()) // manager.get_process_num())
            print(f"{manager.get_total_links()} was expected but "
                  f"{manager.get_total_downloaded_links_count()} was downloaded.")
            manager.done_retries += 1
            print(f"Trying retry {manager.done_retries}")
        else:
            break


def start_threads(links: List[str], maps: Dict[str, str], session: requests.Session,
                  file_path_prefix: str, http2: bool, debug: bool = False, cpu_num: int = 0) -> List[Optional[str]]:
    failed_links = Queue()

    def update_hook(future: Future):
        temp = future.result()
        if temp:
            failed_links.put(temp)

    sent_links = {}

    os.sched_setaffinity(os.getpid(), {cpu_num})

    with ThreadPoolExecutor(max_workers=5) as executor:
        for link in links:
            temp_path = os.path.join(file_path_prefix, maps[link])
            sent_links[link] = temp_path
            thread_future = executor.submit(download_thread, temp_path, link, session, http2)
            thread_future.add_done_callback(update_hook)

    failed = []

    for link in failed_links.queue:
        del sent_links[link]
        failed.append(link)

    send_data = pickle.dumps(list(sent_links.values()))
    msg = f"{'POST_FILENAME_QUEUE':<{HEADER_SIZE}}"
    client = Client(IP, PORT)
    client.send_data(msg)
    client.send_data(send_data, "bytes")

    if debug:
        print(f"Sending data to server of size {len(send_data)} bytes")

    return failed


def download_thread(file_path: str, link: str, session: requests.Session,
                    http2: bool) -> Optional[str]:
    if os.path.exists(file_path):
        return None

    return fetch_data(link, session, 120, file_path, http2)