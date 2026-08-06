ARG BASE_IMAGE=ubuntu:20.04
FROM ${BASE_IMAGE}

# Ubuntu 20.04 build container for kernel and Lustre client builds.
# GCC 9 is the default on Focal Fossa.
#
# KERNEL_DEB_SOURCE comes from targets.yaml (kernel_deb_source field)
# via --build-arg so the apt-installed kernel source package matches
# the kernel ltvm builds against.
ARG KERNEL_DEB_SOURCE=linux-source-5.4.0

ENV DEBIAN_FRONTEND=noninteractive

# Install build packages from the same shared common/packages-dev.txt
# the rocky containers use.  RHEL package names get translated to
# Debian via package-map.txt; "-" means skip.
COPY common/packages-dev.txt   /tmp/packages-dev.txt
COPY ubuntu2004/package-map.txt /tmp/package-map.txt
RUN apt-get update \
    && cat /tmp/packages-dev.txt \
        | grep -v '^\s*#' | grep -v '^\s*$' \
        | sort -u \
        | awk 'NR==FNR { \
                 if ($0 ~ /^[[:space:]]*#/ || $0 ~ /^[[:space:]]*$/) next; \
                 rhel=$1; \
                 sub(/^[^[:space:]]+[[:space:]]+/, ""); \
                 map[rhel]=$0; \
                 next \
               } \
               { if ($1 in map) { if (map[$1] != "-") print map[$1] } \
                 else print $1 }' \
            /tmp/package-map.txt - \
        | tr ' ' '\n' \
        | sort -u \
        | xargs apt-get install -y --no-install-recommends \
    && apt-get install -y --no-install-recommends "${KERNEL_DEB_SOURCE}" \
    && rm -f /tmp/packages-dev.txt /tmp/package-map.txt \
    && rm -rf /var/lib/apt/lists/*

# Whamcloud-patched e2fsprogs (needed for Lustre userspace tools).
# Pinned via build-e2fsprogs.sh's DEFAULT_E2FS_TAG.
#
# focal's linux-libc-dev ships linux/fsverity.h without
# FS_IOC_READ_VERITY_METADATA (added in 5.12), but e2fsprogs guards
# that code on header presence alone, so the probe must be forced
# off or misc/create_inode.c fails to compile.
COPY common/build-e2fsprogs.sh /tmp/build-e2fsprogs.sh
RUN apt-get update && apt-get install -y git ca-certificates \
    && E2FS_CONFIGURE_ARGS=ac_cv_header_linux_fsverity_h=no \
       bash /tmp/build-e2fsprogs.sh \
    && rm -f /tmp/build-e2fsprogs.sh \
    && rm -rf /var/lib/apt/lists/*

# Cross-compilers for both arches.  Debian names gcc for the target's
# GNU triple with hyphens (gcc-x86-64-linux-gnu), unlike RHEL (gcc-
# x86_64-linux-gnu).  Both directions best-effort because the exact
# set installed at container build time depends on the building host's
# arch; the inner build script falls back to a runtime install if the
# cross toolchain is missing.
RUN apt-get update \
    && (apt-get install -y --no-install-recommends \
          gcc-aarch64-linux-gnu g++-aarch64-linux-gnu 2>/dev/null || true) \
    && (apt-get install -y --no-install-recommends \
          gcc-x86-64-linux-gnu g++-x86-64-linux-gnu 2>/dev/null || true) \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/lib/ccache:${PATH}"
ENV CCACHE_DIR="/ccache"

WORKDIR /build
ENTRYPOINT ["/bin/bash"]
