FROM golang:alpine AS builder

RUN mkdir /app
WORKDIR /app
COPY . .

RUN go env -w GO111MODULE=on \
    && go env -w GOPROXY=https://goproxy.cn,direct \
    && go env -w CGO_ENABLED=0 \
    && go env \
    && go mod tidy \
    && go build -o server .

FROM alpine:latest

LABEL MAINTAINER="SliverHorn@sliver_horn@qq.com"
# 设置时区
ENV TZ=Asia/Shanghai
RUN mkdir /app \
    && apk update && apk add --no-cache tzdata openntpd \
    && ln -sf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY --from=0 /app/server ./
COPY --from=0 /app/resource ./resource/
COPY --from=0 /app/config.docker.yaml ./

# 挂载目录：如果使用了sqlite数据库，容器命令示例：docker run -d -v /宿主机路径/gva.db:/app/gva.db -p 8888:8888 --name gva-server-v1 gva-server:1.0
# VOLUME ["/app"]

EXPOSE 8888
ENTRYPOINT ./server -c config.docker.yaml
