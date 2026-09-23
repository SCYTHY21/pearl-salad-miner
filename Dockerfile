# syntax=docker/dockerfile:1
#
# Pearl (PRL) GPU miner image for SaladCloud Container Engine (NVIDIA nodes).
#
# - Base: nvidia/cuda "base" flavour (no CUDA toolkit inside; the miners load
#   libcuda.so.1 / libnvidia-opencl.so.1 from the host driver that SaladCloud
#   mounts into the container).
# - WildRig-Multi : OpenCL miner, 0% devfee on the PearlHash pool. MINER=wildrig
# - SRBMiner      : CUDA/OpenCL miner, 2% devfee on pearlhash, any pool.  MINER=srb
# - krig-miner    : Kryptex's CUDA miner, 0% devfee, Kryptex pool only.    MINER=krig
# MINER accepts a list ("wildrig srb"): the entrypoint tries them in order and
# moves to the next one when a miner dies before producing its first share.
# All archives are pinned by version AND sha256 so a rebuild is reproducible.

FROM nvidia/cuda:12.8.1-base-ubuntu24.04

ARG KRIG_VERSION=1.5.2
ARG KRIG_SHA256=53863c153c7fddf711482de21414392f856ed3472692757887e65b1c7583005e
ARG SRB_VERSION=3.6.9
ARG SRB_VERSION_DASH=3-6-9
ARG SRB_SHA256=3248b62e8bbefea2f5d8330ebac70ec7f93dca4c48484fd0674dd2bf8cbb384c
ARG WILDRIG_VERSION=0.51.2
ARG WILDRIG_SHA256=da1463dcd3444687c7b29b1351e5bd2cb6b7fe204254f12cfac9796a17615c37

ENV DEBIAN_FRONTEND=noninteractive

# ocl-icd-libopencl1 is the OpenCL loader; the NVIDIA ICD entry makes it find the
# driver's libnvidia-opencl.so.1 (mounted when NVIDIA_DRIVER_CAPABILITIES has "compute").
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl ocl-icd-libopencl1 clinfo \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /etc/OpenCL/vendors \
 && echo "libnvidia-opencl.so.1" > /etc/OpenCL/vendors/nvidia.icd

WORKDIR /opt/miners

RUN set -eux; \
    curl -fsSL -o wildrig.tar.gz \
      "https://github.com/andru-kun/wildrig-multi/releases/download/${WILDRIG_VERSION}/wildrig-multi-linux-${WILDRIG_VERSION}.tar.gz"; \
    echo "${WILDRIG_SHA256}  wildrig.tar.gz" | sha256sum -c -; \
    mkdir -p wildrig && tar xzf wildrig.tar.gz -C wildrig && rm wildrig.tar.gz; \
    chmod +x wildrig/wildrig-multi; \
    curl -fsSL -o krig.tar.gz \
      "https://github.com/kryptex/krig-miner/releases/download/v${KRIG_VERSION}/krig-miner-${KRIG_VERSION}-linux-x64.tar.gz"; \
    echo "${KRIG_SHA256}  krig.tar.gz" | sha256sum -c -; \
    mkdir -p krig && tar xzf krig.tar.gz -C krig && rm krig.tar.gz; \
    chmod +x krig/krig-miner; \
    curl -fsSL -o srb.tar.gz \
      "https://github.com/doktor83/SRBMiner-Multi/releases/download/${SRB_VERSION}/SRBMiner-Multi-${SRB_VERSION_DASH}-Linux.tar.gz"; \
    echo "${SRB_SHA256}  srb.tar.gz" | sha256sum -c -; \
    mkdir -p srb && tar xzf srb.tar.gz -C srb --strip-components=1 && rm srb.tar.gz; \
    chmod +x srb/SRBMiner-MULTI

COPY entrypoint.sh /opt/miners/entrypoint.sh

RUN chmod +x /opt/miners/entrypoint.sh \
 && useradd --system --uid 10001 --home-dir /opt/miners --shell /usr/sbin/nologin miner \
 && chown -R miner:miner /opt/miners

USER miner
ENV HOME=/opt/miners \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

LABEL org.opencontainers.image.title="pearl-salad-miner" \
      org.opencontainers.image.description="Pearl (PRL) pearlhash miner for SaladCloud NVIDIA GPUs (WildRig / SRBMiner / krig-miner)" \
      org.opencontainers.image.source="https://github.com/SCYTHY21/pearl-salad-miner"

ENTRYPOINT ["/opt/miners/entrypoint.sh"]
