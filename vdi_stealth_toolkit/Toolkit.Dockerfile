FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive

# 1. 配置国内镜像源 (USTC) - Focal 20.04
RUN echo "deb http://mirrors.ustc.edu.cn/ubuntu/ focal main restricted universe multiverse" > /etc/apt/sources.list && \
    echo "deb http://mirrors.ustc.edu.cn/ubuntu/ focal-security main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb http://mirrors.ustc.edu.cn/ubuntu/ focal-updates main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb http://mirrors.ustc.edu.cn/ubuntu/ focal-backports main restricted universe multiverse" >> /etc/apt/sources.list && \
    apt-get update && apt-get install -y ca-certificates && \
    echo "deb https://mirrors.ustc.edu.cn/ubuntu/ focal main restricted universe multiverse" > /etc/apt/sources.list && \
    echo "deb https://mirrors.ustc.edu.cn/ubuntu/ focal-security main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.ustc.edu.cn/ubuntu/ focal-updates main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.ustc.edu.cn/ubuntu/ focal-backports main restricted universe multiverse" >> /etc/apt/sources.list

# 2. 安装编译依赖
RUN apt-get update && apt-get install -y gcc libc6-dev

WORKDIR /workspace
