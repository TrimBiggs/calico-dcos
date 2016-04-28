#!/usr/bin/env python

# Copyright 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections import OrderedDict

import psutil

_log = logging.getLogger(__name__)


def run_command(command, args=None, paths=None):
    """
    Run a command on the host.
    :return: A tuple of (output, exception).  Either one of output or exception
    will be None.
    """
    args = args or []
    paths = paths or []

    # Locate the command from the possible list of paths (if supplied).
    for path in paths:
        new_command = os.path.join(path, command)
        if os.path.exists(new_command):
            command = new_command
    _log.debug("Executing command: %s", command)

    try:
        res = subprocess.check_output([command] + args)
    except subprocess.CalledProcessError, e:
        _log.exception("CalledProcessError running command: %s", e)
        return None, e
    except OSError, e:
        _log.exception("OSError running command: %s", e)
        return None, e
    else:
        _log.debug("Command output: %s", res)
        return res.strip(), None


def restart_service(service_name, process):
    """
    Restart the process using systemctl if possible (otherwise kill the
    process)
    :param service_name:
    :param process:
    """
    res, exc = run_command("systemctl", args=["restart", service_name],
                           paths=["/usr/bin", "/bin"])

    if exc and isinstance(exc, OSError):
        _log.warning("No systemctl binary - killing process %s", process)
        process.kill()


def start_service(service_name):
    """
    Start the process using systemctl if possible.
    :param service_name:
    """
    run_command("systemctl", args=["start", service_name],
                paths=["/usr/bin", "/bin"])


def ensure_dir(directory):
    """
    Ensure the specified directory exists
    :param directory:
    """
    if not os.path.exists(directory):
        os.makedirs(directory)


def find_process(exe_re, parms_re):
    """
    Find the unique process specified by the executable and command line
    regexes.

    :param exe_re: Regex used to search for a particular executable in the
    process list.  The exe regex is compared against the full command line
    invocation - so executable plus arguments.
    :param parms_re:
    :return: The matching process, or None if no process is found.  If
    multiple matching processes are found, this indicates an error with the
    regex, and the script will terminate.
    """
    processes = []
    for p in psutil.process_iter():
        cmds = p.cmdline()
        if not cmds:
            continue
        if not exe_re.match(cmds[0]):
            continue
        if not parms_re:
            processes.append(p)
            continue
        parms = cmds[1:]
        match = any(parms_re.match(parm) for parm in parms)
        if match:
            processes.append(p)
            continue

    _log.debug("Matched: %s", processes)

    # Multiple processes suggests our query is not correct.
    if len(processes) > 1:
        _log.error("Found multiple matching processes: %s", exe_re)
        sys.exit(1)

    return processes[0] if processes else None


def wait_for_process(exe_re, parms_re, max_wait, stability_time=0,
                     fail_if_not_found=True):
    """
    Locate the specified process, waiting a specified max time before giving
    up.  If the process can not be found within the time limit, the script
    exits.
    :param exe_re: Regex used to search for a particular executable in the
    process list.  The exe regex is compared against the full command line
    invocation - so executable plus arguments.
    :param parms_re:
    :param max_wait:  The maximum time to wait for the process to appear.
    :param stability_time:  The time to wait to check for process stability.
    :param fail_if_not_found: Fail the request if the process is not found
    within the time limit.
    :return: The matching process, or None if no process is found.  If
    multiple matching processes are found, this indicates an error with the
    regex, and the script will terminate.  If no processes are found, this
    also indicates a problem and the script terminates.
    """
    start = time.time()
    process = find_process(exe_re, parms_re)
    while not process and time.time() < start + max_wait:
        time.sleep(1)
        process = find_process(exe_re, parms_re)

    if not process:
        _log.warning("Process not found within timeout: %s", exe_re)
        if fail_if_not_found:
            _log.error("Expecting process to be found, it wasn't")
            sys.exit(1)
        else:
            _log.debug("Returning no process")
            return None

    if stability_time:
        _log.debug("Waiting to check process stability")
        time.sleep(stability_time)
        new_process = find_process(exe_re, parms_re)
        if not new_process:
            _log.error("Process terminated unexpectedly: %s", process)
            sys.exit(1)
        if process.pid != new_process.pid:
            _log.error("Process restarting unexpectedly: %s -> %s",
                       process, new_process)
            sys.exit(1)

    return process


def load_config(filename):
    """
    Load a JSON config file from disk.
    :param filename:  The filename of the config file
    :return:  A dictionary containing the JSON data.  If the file was not found
    an empty dictionary is returned.
    """
    if os.path.exists(filename):
        _log.debug("Reading config file: %s", filename)
        with open(filename) as f:
            config = json.loads(f.read())
    else:
        _log.debug("Config file does not exist: %s", filename)
        config = {}
    _log.debug("Returning config:\n%s", config)
    return config


def store_config(filename, config):
    """
    Store the supplied config as a JSON file.  This performs an atomic write
    to the config file by writing to a temporary file and then renaming the
    file.
    :param filename:  The filename
    :param config:  The config (a simple dictionary)
    """
    ensure_dir(os.path.dirname(filename))
    atomic_write(filename, json.dumps(config))


def load_property_file(filename):
    """
    Loads a file containing x=a,b,c... properties separated by newlines, and
    returns an OrderedDict where the key is x and the value is [a,b,c...]
    :param filename:
    :return:
    """
    props = OrderedDict()
    if not os.path.exists(filename):
        return props
    with open(filename) as f:
        for line in f:
            line = line.strip().split("=", 1)
            if len(line) != 2:
                continue
            props[line[0].strip()] = line[1].split(",")
    _log.debug("Read property file:\n%s", props)
    return props


def store_property_file(filename, props):
    """
    Write a property file (see load_property_file)
    :param filename:
    :param props:
    """
    config = "\n".join(prop + "=" + ",".join(vals)
                       for prop, vals in props.iteritems())
    atomic_write(filename, config)


def atomic_write(filename, contents):
    """
    Atomic write a file, by first writing out a temporary file and then
    moving into place.  The temporary filename is simply the supplied
    filename with ".tmp" appended.
    :param filename:
    :param contents:
    """
    ensure_dir(os.path.dirname(filename))
    tmp = filename + ".tmp"
    with open(tmp, "w") as f:
        f.write(contents)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, filename)


def move_file_if_missing(from_file, to_file):
    """
    Checks if the destination file exists and if not moves it into place.
    :param from_file:
    :param to_file:
    :return: Whether the file was moved.
    """
    _log.debug("Move file from %s to %s", from_file, to_file)
    if not os.path.exists(from_file):
        _log.error("From file does not exist.")
        return False
    if os.path.exists(to_file):
        _log.debug("File %s already exists, not copying", to_file)
        return False
    tmp_to_file = to_file + ".tmp"

    # We cannot use os.rename() because that does not work across devices.  To
    # ensure we still do an atomic move, copy the file to a temporary location
    # and then rename it.
    ensure_dir(os.path.dirname(to_file))
    shutil.move(from_file, tmp_to_file)
    os.rename(tmp_to_file, to_file)
    return True
