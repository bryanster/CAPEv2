#!/usr/bin/env python
# Copyright (C) 2010-2015 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.
from __future__ import absolute_import
import argparse
import gc
import json
import logging
import multiprocessing
import os
import platform
import resource
import signal
import sys
import time

if sys.version_info[:2] < (3, 6):
    sys.exit("You are running an incompatible version of Python, please use >= 3.6")

try:
    import pebble
except ImportError:
    sys.exit("Missed dependency: pip3 install Pebble")

log = logging.getLogger()

sys.path.append(os.path.join(os.path.abspath(os.path.dirname(__file__)), ".."))
from concurrent.futures import TimeoutError

from lib.cuckoo.common.colors import red
from lib.cuckoo.common.config import Config
from lib.cuckoo.common.constants import CUCKOO_ROOT
from lib.cuckoo.common.utils import free_space_monitor
from lib.cuckoo.core.database import TASK_COMPLETED, TASK_FAILED_PROCESSING, TASK_REPORTED, Database, Task
from lib.cuckoo.core.plugins import RunProcessing, RunReporting, RunSignatures
from lib.cuckoo.core.startup import ConsoleHandler, check_linux_dist, init_modules, init_yara

cfg = Config()
repconf = Config("reporting")
if repconf.mongodb.enabled:
    from bson.objectid import ObjectId

    from dev_utils.mongodb import mongo_find, mongo_find_one

if repconf.elasticsearchdb.enabled and not repconf.elasticsearchdb.searchonly:
    from elasticsearch.exceptions import RequestError as ESRequestError

    from dev_utils.elasticsearchdb import elastic_handler, get_analysis_index, get_query_by_info_id

    es = elastic_handler

check_linux_dist()

pending_future_map = {}
pending_task_id_map = {}

# https://stackoverflow.com/questions/41105733/limit-ram-usage-to-python-program
def memory_limit(percentage: float = 0.8):
    if platform.system() != "Linux":
        print("Only works on linux!")
        return
    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (int(get_memory() * 1024 * percentage), hard))


def get_memory():
    with open("/proc/meminfo", "r") as mem:
        free_memory = 0
        for i in mem:
            sline = i.split()
            if str(sline[0]) == "MemAvailable:":
                free_memory = int(sline[1])
                break
    return free_memory


def process(target=None, copy_path=None, task=None, report=False, auto=False, capeproc=False, memory_debugging=False):
    # This is the results container. It's what will be used by all the
    # reporting modules to make it consumable by humans and machines.
    # It will contain all the results generated by every processing
    # module available. Its structure can be observed through the JSON
    # dump in the analysis' reports folder. (If jsondump is enabled.)
    task_dict = task.to_dict() or {}
    task_id = task_dict.get("id") or 0
    results = {"statistics": {"processing": [], "signatures": [], "reporting": []}}
    if memory_debugging:
        gc.collect()
        log.info("[%s] (1) GC object counts: %d, %d", task_id, len(gc.get_objects()), len(gc.garbage))
    if memory_debugging:
        gc.collect()
        log.info("[%s] (2) GC object counts: %d, %d", task_id, len(gc.get_objects()), len(gc.garbage))
    RunProcessing(task=task_dict, results=results).run()
    if memory_debugging:
        gc.collect()
        log.info("[%s] (3) GC object counts: %d, %d", task_id, len(gc.get_objects()), len(gc.garbage))

    RunSignatures(task=task_dict, results=results).run()
    if memory_debugging:
        gc.collect()
        log.info("[%s] (4) GC object counts: %d, %d", task_id, len(gc.get_objects()), len(gc.garbage))

    if report:
        if auto or capeproc:
            reprocess = False
        else:
            reprocess = report

        RunReporting(task=task.to_dict(), results=results, reprocess=reprocess).run()
        Database().set_status(task_id, TASK_REPORTED)

        if auto:
            if cfg.cuckoo.delete_original and os.path.exists(target):
                os.unlink(target)

            if copy_path is not None and cfg.cuckoo.delete_bin_copy and os.path.exists(copy_path):
                os.unlink(copy_path)

    if memory_debugging:
        gc.collect()
        log.info("[%s] (5) GC object counts: %d, %d", task_id, len(gc.get_objects()), len(gc.garbage))
        for i, obj in enumerate(gc.garbage):
            log.info("[%s] (garbage) GC object #%d: type=%s", task_id, i, type(obj).__name__)


def init_worker():
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def init_logging(auto=False, tid=0, debug=False):
    formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    ch = ConsoleHandler()
    ch.setFormatter(formatter)
    log.addHandler(ch)
    try:
        if not os.path.exists(os.path.join(CUCKOO_ROOT, "log")):
            os.makedirs(os.path.join(CUCKOO_ROOT, "log"))
        if auto:
            if cfg.log_rotation.enabled:
                days = cfg.log_rotation.backup_count or 7
                fh = logging.handlers.TimedRotatingFileHandler(
                    os.path.join(CUCKOO_ROOT, "log", "process.log"), when="midnight", backupCount=int(days)
                )
            else:
                fh = logging.handlers.WatchedFileHandler(os.path.join(CUCKOO_ROOT, "log", "process.log"))
        else:
            fh = logging.handlers.WatchedFileHandler(os.path.join(CUCKOO_ROOT, "log", "process-%s.log" % tid))

    except PermissionError:
        sys.exit("Probably executed with wrong user, PermissionError to create/access log")

    fh.setFormatter(formatter)
    log.addHandler(fh)

    if debug:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)

    logging.getLogger("urllib3").setLevel(logging.WARNING)


def processing_finished(future):
    task_id = pending_future_map.get(future)
    try:
        result = future.result()
        log.info("Task #%d: reports generation completed", task_id)
    except TimeoutError as error:
        log.error("Processing Timeout %s - Task ID: %d", error, task_id)
        Database().set_status(task_id, TASK_FAILED_PROCESSING)
    except pebble.ProcessExpired as error:
        log.error("Exception when processing task %s: %s", task_id, error)
        Database().set_status(task_id, TASK_FAILED_PROCESSING)
    except Exception as error:
        log.error("Exception when processing task %s: %s", task_id, error)
        Database().set_status(task_id, TASK_FAILED_PROCESSING)

    del pending_future_map[future]
    del pending_task_id_map[task_id]


def autoprocess(parallel=1, failed_processing=False, maxtasksperchild=7, memory_debugging=False, processing_timeout=300):
    maxcount = cfg.cuckoo.max_analysis_count
    count = 0
    db = Database()
    # pool = multiprocessing.Pool(parallel, init_worker)
    try:
        memory_limit()
        log.info("Processing analysis data")
        with pebble.ProcessPool(max_workers=parallel, max_tasks=maxtasksperchild, initializer=init_worker) as pool:
            # CAUTION - big ugly loop ahead.
            while count < maxcount or not maxcount:

                # If not enough free disk space is available, then we print an
                # error message and wait another round (this check is ignored
                # when the freespace configuration variable is set to zero).
                if cfg.cuckoo.freespace:
                    # Resolve the full base path to the analysis folder, just in
                    # case somebody decides to make a symbolic link out of it.
                    dir_path = os.path.join(CUCKOO_ROOT, "storage", "analyses")
                    need_space, space_available = free_space_monitor(dir_path, return_value=True, processing=True)
                    if need_space:
                        log.error(
                            "Not enough free disk space! (Only %d MB!). You can change limits it in cuckoo.conf -> freespace",
                            space_available,
                        )
                        time.sleep(60)
                        continue

                # If still full, don't add more (necessary despite pool).
                if len(pending_task_id_map) >= parallel:
                    time.sleep(5)
                    continue
                if failed_processing:
                    tasks = db.list_tasks(status=TASK_FAILED_PROCESSING, limit=parallel, order_by=Task.completed_on.asc())
                else:
                    tasks = db.list_tasks(status=TASK_COMPLETED, limit=parallel, order_by=Task.completed_on.asc())
                added = False
                # For loop to add only one, nice. (reason is that we shouldn't overshoot maxcount)
                for task in tasks:
                    # Not-so-efficient lock.
                    if pending_task_id_map.get(task.id):
                        continue
                    log.info("Processing analysis data for Task #%d", task.id)
                    if task.category != "url":
                        sample = db.view_sample(task.sample_id)
                        copy_path = os.path.join(CUCKOO_ROOT, "storage", "binaries", str(task.id), sample.sha256)
                    else:
                        copy_path = None
                    args = task.target, copy_path
                    kwargs = dict(report=True, auto=True, task=task, memory_debugging=memory_debugging)
                    if memory_debugging:
                        gc.collect()
                        log.info("[%d] (before) GC object counts: %d, %d", task.id, len(gc.get_objects()), len(gc.garbage))
                    # result = pool.apply_async(process, args, kwargs)
                    future = pool.schedule(process, args, kwargs, timeout=processing_timeout)
                    pending_future_map[future] = task.id
                    pending_task_id_map[task.id] = future
                    future.add_done_callback(processing_finished)
                    if memory_debugging:
                        gc.collect()
                        log.info("[%d] (after) GC object counts: %d, %d", task.id, len(gc.get_objects()), len(gc.garbage))
                    count += 1
                    added = True
                    if copy_path != None:
                        copy_origin_path = os.path.join(CUCKOO_ROOT, "storage", "binaries", sample.sha256)
                        if cfg.cuckoo.delete_bin_copy and os.path.exists(copy_origin_path):
                            os.unlink(copy_origin_path)
                    break
                if not added:
                    # don't hog cpu
                    time.sleep(5)
    except KeyboardInterrupt:
        # ToDo verify in finally
        # pool.terminate()
        raise
    except MemoryError:
        mem = get_memory() / 1024 / 1024
        print("Remain: %.2f GB" % mem)
        sys.stderr.write("\n\nERROR: Memory Exception\n")
        sys.exit(1)
    except Exception as e:
        import traceback

        traceback.print_exc()
    finally:
        pool.close()
        pool.join()


def _load_report(task_id: int, return_one: bool = False):

    if repconf.mongodb.enabled:
        if return_one:
            analysis = mongo_find_one("analysis", {"info.id": int(task_id)}, sort=[("_id", -1)])
            for process in analysis.get("behavior", {}).get("processes", []):
                calls = []
                for call in process["calls"]:
                    calls.append(ObjectId(call))
                process["calls"] = []
                for call in mongo_find("calls", {"_id": {"$in": calls}}, sort=[("_id", 1)]) or []:
                    process["calls"] += call["calls"]
            return analysis

        else:
            return mongo_find("analysis", {"info.id": int(task_id)})

    if repconf.elasticsearchdb.enabled and not repconf.elasticsearchdb.searchonly:
        try:
            analyses = (
                es.search(index=get_analysis_index(), query=get_query_by_info_id(task_id), sort={"info.id": {"order": "desc"}})
                .get("hits", {})
                .get("hits", [])
            )
            if analyses:
                if return_one:
                    return analyses[0]
                else:
                    return analyses
        except ESRequestError as e:
            print(e)

    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("id", type=str, help="ID of the analysis to process (auto for continuous processing of unprocessed tasks).")
    parser.add_argument("-c", "--caperesubmit", help="Allow CAPE resubmit processing.", action="store_true", required=False)
    parser.add_argument("-d", "--debug", help="Display debug messages", action="store_true", required=False)
    parser.add_argument("-r", "--report", help="Re-generate report", action="store_true", required=False)
    parser.add_argument(
        "-p", "--parallel", help="Number of parallel threads to use (auto mode only).", type=int, required=False, default=1
    )
    parser.add_argument(
        "-fp", "--failed-processing", help="reprocess failed processing", action="store_true", required=False, default=False
    )
    parser.add_argument(
        "-mc", "--maxtasksperchild", help="Max children tasks per worker", action="store", type=int, required=False, default=7
    )
    parser.add_argument(
        "-md",
        "--memory-debugging",
        help="Enable logging garbage collection related info",
        action="store_true",
        required=False,
        default=False,
    )
    parser.add_argument(
        "-pt",
        "--processing-timeout",
        help="Max amount of time spent in processing before we fail a task",
        action="store",
        type=int,
        required=False,
        default=300,
    )
    testing_args = parser.add_argument_group("Signature testing options")
    testing_args.add_argument(
        "-sig",
        "--signatures",
        help="Re-execute signatures on the report, doesn't work for signature with self.get_raw_argument, use self.get_argument",
        action="store_true",
        default=False,
        required=False,
    )
    testing_args.add_argument(
        "-sn",
        "--signature-name",
        help="Run only one signature. To be used with --signature. Example -sig -sn cape_detected_threat",
        action="store",
        default=False,
        required=False,
    )
    testing_args.add_argument(
        "-jr",
        "--json-report",
        help="Path to json report, only if data not in mongo/default report location",
        action="store",
        default=False,
        required=False,
    )
    args = parser.parse_args()

    init_yara()
    init_modules()
    if args.id == "auto":
        init_logging(auto=True, debug=args.debug)
        autoprocess(
            parallel=args.parallel,
            failed_processing=args.failed_processing,
            maxtasksperchild=args.maxtasksperchild,
            memory_debugging=args.memory_debugging,
            processing_timeout=args.processing_timeout,
        )
    else:
        if not os.path.exists(os.path.join(CUCKOO_ROOT, "storage", "analyses", args.id)):
            sys.exit(red("\n[-] Analysis folder doesn't exist anymore\n"))
        init_logging(tid=args.id, debug=args.debug)
        task = Database().view_task(int(args.id))
        if args.signatures:
            report = False
            results = _load_report(int(args.id), return_one=True)
            if not results:
                # fallback to json
                report = os.path.join(CUCKOO_ROOT, "storage", "analyses", args.id, "reports", "report.json")
                if not os.path.exists(report):
                    if args.json_report and not os.path.exists(args.json_report):
                        report = args.json_report
                    else:
                        sys.exit("File {} doest exist".format(report))
                if report:
                    results = json.load(open(report))
            if results is not None:
                RunSignatures(task=task.to_dict(), results=results).run(args.signature_name)
        else:
            process(task=task, report=args.report, capeproc=args.caperesubmit, memory_debugging=args.memory_debugging)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
