.PHONY: netmodules

CALICO_MESOS_FILES=./executor/executor.py

default:
	docker build -t timothydbriggs/calico-dcos -f Dockerfile.framework .

run: 
	docker run -it --net=host \
	-e CALICO_CALICOCTL_URL=https://github.com/projectcalico/calico-containers/releases/download/v0.18.0/calicoctl \
    -e CALICO_NODE_IMG=calico/node:v0.18.0 \
    -e CALICO_LIBNETWORK_IMG=calico/node-libnetwork:v0.8.0 \
    -e CALICO_ALLOW_RESTART_AGENT=true \
    -e CALICO_ALLOW_RESTART_DOCKER=true \
    -e CALICO_MAX_CONCURRENT_RESTARTS=1 \
    -e CALICO_ZK=zk://192.168.65.90:2181/calico \
    -e CALICO_CPU_LIMIT_INSTALL=0.1 \
    -e CALICO_MEM_LIMIT_INSTALL=1024 \
    -e CALICO_CPU_LIMIT_ETCD_PROXY=0.1 \
    -e CALICO_MEM_LIMIT_ETCD_PROXY=1024 \
    -e CALICO_CPU_LIMIT_NODE=0.1 \
    -e CALICO_MEM_LIMIT_NODE=1024 \
    -e CALICO_CPU_LIMIT_LIBNETWORK=0.1 \
    -e CALICO_MEM_LIMIT_LIBNETWORK=1024 \
    -e CALICO_EXECUTOR_URL=https://transfer.sh/YdeMF/executor \
    -e CALICO_MESOS_PLUGIN=https://github.com/projectcalico/calico-mesos/releases/download/v0.1.5/calico_mesos \
    -e ETCD_SRV=etcd.mesos \
    -e ETCD_BINARY_URL=https://github.com/coreos/etcd/releases/download/v2.3.1/etcd-v2.3.1-linux-amd64.tar.gz \
    -e MESOS_MASTER=zk://192.168.65.90:2181/mesos \
    -e CALICO_ALLOW_DOCKER_UPDATE=true \
    -e CALICO_ALLOW_AGENT_UPDATE=true \
    -e LIBPROCESS_IP=192.168.99.100 \
    calico/calico-dcos

push:
	docker push timothydbriggs/calico-dcos:installer

executor: dist/executor
dist/executor: $(CALICO_MESOS_FILES)
	mkdir -p -m 777 dist/
	# Build the mesos plugin
	docker build -t calico/dcos-builder -f Dockerfile.executor .
	docker run --rm \
         -v `pwd`/executor/:/code/executor \
         -v `pwd`/dist/:/code/dist \
	     calico/dcos-builder \
	     pyinstaller executor/executor.py -ayF
	curl --upload-file dist/executor https://transfer.sh/executor

ec: executor-container
executor-container:
	docker build -t timothydbriggs/executor -f Dockerfile.executor .

netmodules:
	tar -czf netmodules.tar.gz netmodules/*
	curl --upload-file ./netmodules.tar.gz https://transfer.sh/netmodules.tar.gz
