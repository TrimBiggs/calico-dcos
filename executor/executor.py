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

import logging
import os
import re
import socket
import subprocess
import sys
import threading
import mesos.interface
from mesos.interface import mesos_pb2
import mesos.native

from utils import (run_command, wait_for_process, store_config, load_config,
                   move_file_if_missing, store_property_file,
                   load_property_file, start_service, restart_service)


_log = logging.getLogger(__name__)

# Calico installation config files
EXECUTOR_CONFIG_DIR = "/etc/calico/executor"
NETMODULES_INSTALL_CONFIG = EXECUTOR_CONFIG_DIR + "/netmodules"
DOCKER_INSTALL_CONFIG = EXECUTOR_CONFIG_DIR + "/docker"

# Docker information for a standard Docker install.
DOCKER_DAEMON_EXE_RE = re.compile(r"(.*/)?docker")
DOCKER_DAEMON_PARMS_RE = re.compile(r"(--)?daemon")
DOCKER_DAEMON_CONFIG = "/etc/docker/daemon.json"

# Docker information for a standard Docker install.
AGENT_EXE_RE = re.compile(r"(.*/)?mesos-slave")
AGENT_EXE_PARMS_RE = None
AGENT_CONFIG = "/opt/mesosphere/etc/mesos-slave-common"
AGENT_MODULES_CONFIG = "/opt/mesosphere/etc/mesos-slave-modules.json"

# Docker version regex
DOCKER_VERSION_RE = re.compile(r"Docker version (\d+)\.(\d+)\.(\d+).*")

# Netmodules information for determining correct netmodules.so
DISTRO_INFO_FILE = "/etc/os-release"
NETWORK_ISOLATOR_SO_BASENAME = "libmesos_network_isolator"

# Fixed address for our etcd proxy.
CLUSTER_STORE_ETCD_PROXY = "etcd://127.0.0.1:2379"

# Max time for process restarts (in seconds)
MAX_TIME_FOR_DOCKER_RESTART = 30
MAX_TIME_FOR_AGENT_RESTART = 30

# Time to wait to check that a process is stable.
PROCESS_STABILITY_TIME = 5


def initialize_logging():
    """
    Initialise logging to stdout.
    """
    logger = logging.getLogger('')
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
                '%(asctime)s [%(levelname)s]\t%(name)s %(lineno)d: %(message)s')
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(formatter)
    logger.addHandler(handler)


class ExecutorTask(object):
    def __init__(self, task):
        self.id = task.task_id.value
        self.labels = {label.key: label.value for label in task.labels.labels}
        for resource in task.resources:
            if resource.name == "ports":
                port_range = resource.ranges.range[:1].pop()
                self.port = port_range.begin

    def send_update(self, state, message=None, data=None):
        update = mesos_pb2.TaskStatus()
        update.task_id.value = self.id
        update.state = state
        update.message = message or ''
        update.data = data or ''
        if state in [mesos_pb2.TASK_FAILED,
                     mesos_pb2.TASK_ERROR,
                     mesos_pb2.TASK_LOST]:
            update.healthy = False
        else:
            update.healthy = True
        driver.sendStatusUpdate(update)

    def run_pre_task(self):
        print "Running task %s" % self.id
        self.send_update(mesos_pb2.TASK_RUNNING)

        print subprocess.check_output(["ip", "addr"])

    def run_task(self):
        raise NotImplementedError

    def start(self):
        try:
            self.run_pre_task()
        except Exception as e:
            self.send_update(mesos_pb2.TASK_ERROR, message=str(e))
            return
        else:
            try:
                data = self.run_task()
            except Exception as e:
                self.send_update(mesos_pb2.TASK_FAILED, message=str(e))
            else:
                self.send_update(mesos_pb2.TASK_FINISHED, data=data)


class ExecutorNetmodulesTask(ExecutorTask):
    def get_host_info(self):
        """
        Gather information from the host OS.
        :return: A tuple containing the mesos-version, distribution name, architecture,
          in that order. Contents of any of the three tuple values may be None if
          we were unable to detect them.
        """
        _log.info("Gathering host information.")

        # Get Architecture
        arch, _ = run_command("uname", args=["-m"], paths=["/usr/bin", "/bin"])
        _log.info("Arch: %s", arch)

        # Get Mesos Version
        raw_mesos_version, _ = run_command("mesos-slave", args=["--version"],
                                           paths=["/opt/mesosphere/bin"])
        mesos_version = raw_mesos_version.split(" ")[1] if raw_mesos_version else None
        _log.info("Mesos Version: %s" % mesos_version)

        # Get Distribution
        distro = None
        release_info = load_property_file(DISTRO_INFO_FILE)
        if release_info:
            if 'ID' in release_info:
                # Remove quotes surrounding distro
                distro = release_info['ID'][0].replace('\"', '')
            else:
                _log.error("Couldn't find ID field in %s", DISTRO_INFO_FILE)
        else:
            _log.error("Couldn't find release-info file: %s", DISTRO_INFO_FILE)

        _log.info("Distro: %s", distro)
        return mesos_version, distro, arch

    def run_task(self):
        """
        Install netmodules and Calico plugin.  A successful completion of the task
        indicates successful installation.
        """
        results = {}
        # Load the current Calico install info for Docker, and the current Docker
        # daemon configuration.
        install_config = load_config(NETMODULES_INSTALL_CONFIG)
        modules_config = load_config(AGENT_MODULES_CONFIG)
        mesos_props = load_property_file(AGENT_CONFIG)

        # Before starting the install, check that we are able to locate the agent
        # process - if not there is not much we can do here.
        if not install_config:
            _log.debug("Have not started installation yet")
            # noinspection PyTypeChecker
            agent_process = wait_for_process(AGENT_EXE_RE,
                                             AGENT_EXE_PARMS_RE,
                                             MAX_TIME_FOR_AGENT_RESTART,
                                             fail_if_not_found=False)
            if not agent_process:
                _log.info("Cannot find agent process - do not update config")
                return results

        libraries = modules_config.setdefault("libraries", [])
        files = [library.get("file") for library in libraries]
        if "/opt/mesosphere/lib/libmesos_network_isolator.so" not in files:
            # Construct the desired netmodules.so file based on the host's base information
            mesos_version, distro, arch = self.get_host_info()
            if not mesos_version or not distro or not arch:
                _log.error("Unrecognizable System. Performing a no-op for netmodules.")
                return results

            network_isolator_so = "netmodules/{}-{}-{}-{}.so".format(
                NETWORK_ISOLATOR_SO_BASENAME,
                mesos_version,
                distro,
                arch)

            if not os.path.exists(network_isolator_so):
                _log.error("No matching netmodules found for this system: %s" % network_isolator_so)
                # No-op to skip the netmodules installation.
                return results

            # Flag that modules need to be updated and reset the agent create
            # time to ensure we restart the agent.
            _log.debug("Configure netmodules and calico in Mesos")
            install_config["modules-updated"] = True
            install_config["agent-created"] = None
            store_config(NETMODULES_INSTALL_CONFIG, install_config)

            # Copy the netmodules .so and the calico binary.
            move_file_if_missing(
                network_isolator_so,
                "/opt/mesosphere/lib/libmesos_network_isolator.so"
            )
            move_file_if_missing(
                "./calico_mesos",
                "/calico/calico_mesos"
            )

            # Update the modules config to reference the .so
            new_library = {
              "file": "/opt/mesosphere/lib/libmesos_network_isolator.so",
              "modules": [
                {
                  "name": "com_mesosphere_mesos_NetworkIsolator",
                  "parameters": [
                    {
                      "key": "isolator_command",
                      "value": "/calico/calico_mesos"
                    },
                    {
                      "key": "ipam_command",
                      "value": "/calico/calico_mesos"
                    }
                  ]
                },
                {
                  "name": "com_mesosphere_mesos_NetworkHook"
                }
              ]
            }
            libraries.append(new_library)
            store_config(AGENT_MODULES_CONFIG, modules_config)

        hooks = mesos_props.setdefault("MESOS_HOOKS", [])
        isolation = mesos_props.setdefault("MESOS_ISOLATION", [])
        if "com_mesosphere_mesos_NetworkHook" not in hooks:
            # Flag that properties need to be updated and reset the agent create
            # time to ensure we restart the agent.
            _log.debug("Configure mesos properties")
            install_config["properties-updated"] = True
            install_config["agent-created"] = None
            store_config(NETMODULES_INSTALL_CONFIG, install_config)

            # Finally update the properties.  We do this last, because this is what
            # we check to see if files are copied into place.
            isolation.append("com_mesosphere_mesos_NetworkIsolator")
            hooks.append("com_mesosphere_mesos_NetworkHook")
            store_property_file(AGENT_CONFIG, mesos_props)

        # If nothing was updated then exit.
        if not install_config.get("modules-updated") and \
           not install_config.get("properties-updated"):
            _log.debug("NetworkHook not updated by Calico")
            return results

        # If we haven't stored the current agent creation time, then do so now.
        # We use this to track when the agent has restarted with our new config.
        if not install_config.get("agent-created"):
            _log.debug("Store agent creation time")
            # noinspection PyTypeChecker
            agent_process = wait_for_process(AGENT_EXE_RE,
                                             AGENT_EXE_PARMS_RE,
                                             MAX_TIME_FOR_AGENT_RESTART)
            install_config["agent-created"] = str(agent_process.create_time())
            store_config(NETMODULES_INSTALL_CONFIG, install_config)

        # Check the agent process creation time to see if it has been restarted
        # since the config was updated.
        _log.debug("Restart agent if not using updated config")
        # noinspection PyTypeChecker
        agent_process = wait_for_process(AGENT_EXE_RE,
                                         AGENT_EXE_PARMS_RE,
                                         MAX_TIME_FOR_AGENT_RESTART,
                                         PROCESS_STABILITY_TIME)
        if install_config["agent-created"] == str(agent_process.create_time()):
            # The agent has not been restarted, so restart it now.  This will cause
            # the task to fail (but sys.exit(1) to make sure).  The task will be
            # relaunched, and next time will miss this branch and succeed.
            _log.warning("Restarting agent process: %s", agent_process)
            restart_service("dcos-mesos-slave", agent_process)
            sys.exit(1)

        _log.debug("Agent was restarted and is stable since config was updated")
        return results


class ExecutorDockerTask(ExecutorTask):
    def docker_version_supported(self):
        """
        Check if the host has a version of Docker that is supported.
        :return: True if supported.
        """
        res, exc = run_command("docker", args=["--version"],
                               paths=["/usr/bin", "/bin"])

        if exc:
            _log.warning("Unable to query Docker version")
            return False

        version_match = DOCKER_VERSION_RE.match(res)
        if not version_match:
            _log.warning("Docker version output unexpected format")
            return False

        vmajor = int(version_match.group(1))
        vminor = int(version_match.group(2))
        vpatch = int(version_match.group(3))
        if (vmajor, vminor, vpatch) < (1, 10, 0):
            _log.warning("Docker version is not supported")
            return False

        _log.debug("Docker version is supported")
        return True

    def run_task(self):
        """
        Install Docker configuration for Docker multi-host networking.  A successful
        completion of the task indicates successful installation.
        """
        results = {}

        # Check if the Docker version is supported.  If not, just finish the task.
        if not self.docker_version_supported():
            _log.debug("Docker version is not supported - finish task")
            return results

        # Load the current Calico install info for Docker, and the current Docker
        # daemon configuration.
        install_config = load_config(DOCKER_INSTALL_CONFIG)
        daemon_config = load_config(DOCKER_DAEMON_CONFIG)

        # Before starting the install, check that we are able to locate the Docker
        # process.  If necessary wait.
        if not install_config:
            _log.debug("Have not started installation yet")
            daemon_process = wait_for_process(DOCKER_DAEMON_EXE_RE,
                                              DOCKER_DAEMON_PARMS_RE,
                                              MAX_TIME_FOR_DOCKER_RESTART,
                                              fail_if_not_found=False)
            if not daemon_process:
                _log.info("Docker daemon is not running - do not update config")
                return results

        if "cluster-store" not in daemon_config:
            # Before updating the config flag that config is updated, but don't yet
            # put in the create time (we only do that after actually updating the
            # config.
            _log.debug("Configure cluster store in daemon config")
            install_config["docker-updated"] = True
            install_config["docker-created"] = None
            store_config(DOCKER_INSTALL_CONFIG, install_config)

            # Update the daemon config.
            daemon_config["cluster-store"] = CLUSTER_STORE_ETCD_PROXY
            store_config(DOCKER_DAEMON_CONFIG, daemon_config)

        # If Docker was already configured to use a cluster store, and not by
        # Calico, exit now.
        if not install_config.get("docker-updated"):
            _log.debug("Docker not updated by Calico")
            return results

        # If Docker config was updated, store the current Docker process creation
        # time so that we can identify when Docker is restarted.
        if not install_config.get("docker-created"):
            _log.debug("Store docker daemon creation time")
            daemon_process = wait_for_process(DOCKER_DAEMON_EXE_RE,
                                              DOCKER_DAEMON_PARMS_RE,
                                              MAX_TIME_FOR_DOCKER_RESTART)
            install_config["docker-created"] = str(daemon_process.create_time())
            store_config(DOCKER_INSTALL_CONFIG, install_config)

        # Check the Docker daemon is running.
        daemon_process = wait_for_process(DOCKER_DAEMON_EXE_RE,
                                          DOCKER_DAEMON_PARMS_RE,
                                          MAX_TIME_FOR_DOCKER_RESTART,
                                          PROCESS_STABILITY_TIME,
                                          fail_if_not_found=False)
        if not daemon_process and \
           not install_config.get("docker-attempted-start"):
            # No Docker daemon process - it must have failed to come back.
            # We haven't tried a systemctl start so try one now.
            _log.error("Docker process failed after restart - attempt start")
            start_service("docker")
            install_config["docker-attempted-restart"] = True
            store_config(DOCKER_INSTALL_CONFIG, install_config)
            daemon_process = wait_for_process(DOCKER_DAEMON_EXE_RE,
                                              DOCKER_DAEMON_PARMS_RE,
                                              MAX_TIME_FOR_DOCKER_RESTART,
                                              PROCESS_STABILITY_TIME)

        if install_config["docker-created"] == str(daemon_process.create_time()):
            # Docker has not been restarted, so restart it now.  This will cause
            # the task to fail (but sys.exit(1) to make sure).  The task will be
            # relaunched, and next time will miss this branch and succeed.
            _log.warning("Restarting Docker process: %s", daemon_process)
            restart_service("docker", daemon_process)
            sys.exit(1)

        _log.debug("Docker was restarted and is stable since adding cluster store")
        return results


class ExecutorGetIpTask(ExecutorTask):
    def run_task(self):
        """
        Connects a socket to the DNS entry for mesos master and returns
        which IP address it connected via, which should be the agent's
        accessible IP.

        Prints the IP to stdout
        """
        results = {}
        # A comma separated list of host:port is supplied as the only argument.
        for host, port in (hp.split(":") for hp in sys.argv[2].split(",")):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect((host, int(port)))
                our_ip = s.getsockname()[0]
                s.close()
                print our_ip
                return results
            except socket.gaierror:
                continue
        _log.error("Failed to connect to any of: %s", sys.argv[2])
        sys.exit(1)


class Executor(mesos.interface.Executor):
    def __init__(self, task_executor):
        self.task_executor = task_executor
        super(Executor, self).__init__()

    def launchTask(self, driver, task):
        # Create a thread to run the task
        # print task['resources']

        thread = threading.Thread(target=self.task_executor(task).start)
        thread.start()

    def frameworkMessage(self, driver, message):
        """
        Respond to messages sent from the Framework.
        In this case, we'll just echo the message back.
        """
        driver.sendFrameworkMessage(message)


if __name__ == "__main__":
    print "Starting executor for %s" % sys.argv[1]

    initialize_logging()
    task_type = sys.argv[1]
    if task_type == 'netmodules':
        task_executor = ExecutorNetmodulesTask
    elif task_type == 'docker':
        task_executor = ExecutorDockerTask
    elif task_type == 'ip':
        task_executor = ExecutorGetIpTask
    else:
        print "Unexpected task type: %s" % task_type
        sys.exit(1)

    driver = mesos.native.MesosExecutorDriver(Executor(task_executor))
    sys.exit(0 if driver.run() == mesos_pb2.DRIVER_STOPPED else 1)
