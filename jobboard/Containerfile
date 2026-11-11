# Sandbox for the local coding agent. Rootless Podman; sees only /work, its own home volume and Basalt on localhost:8080.
FROM docker.io/library/ubuntu:26.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl wget git git-lfs openssh-client unzip zip xz-utils file jq ripgrep less nano \
      build-essential cmake ninja-build pkg-config python3 python3-venv python3-pip \
      nodejs npm openjdk-21-jdk-headless gh \
    && rm -rf /var/lib/apt/lists/*
# Agent programs: Claude Code (Mode 1 cloud + Mode 2 local) and Qwen Code (Mode 2 local)
RUN npm install -g @anthropic-ai/claude-code @qwen-code/qwen-code && npm cache clean --force
# Android command-line tools; SDK packages go in the android-sdk volume via android-setup
RUN mkdir -p /opt/android-cmdline && cd /opt/android-cmdline \
    && curl -fsSLo t.zip https://dl.google.com/android/repository/commandlinetools-linux-13114758_latest.zip \
    && unzip -q t.zip && rm t.zip
ENV ANDROID_HOME=/opt/android-sdk ANDROID_SDK_ROOT=/opt/android-sdk JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 \
    GRADLE_USER_HOME=/home/agent/.gradle PATH=/opt/agent/bin:/opt/android-cmdline/cmdline-tools/bin:/opt/android-sdk/platform-tools:$PATH
COPY files/ /opt/agent/
RUN chmod +x /opt/agent/bin/*
WORKDIR /work
ENTRYPOINT ["/opt/agent/bin/entrypoint"]
CMD ["bash"]
