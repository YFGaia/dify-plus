FROM node:20-slim AS base

WORKDIR /gva_web/
COPY . .
#RUN sed -i 's/mirrors.aliyun.com/dl-cdn.alpinelinux.org/g' /etc/apk/repositories
RUN yarn config set registry https://registry.npmmirror.com \
    && yarn \
    && yarn build

FROM nginx:alpine
LABEL MAINTAINER="SliverHorn@sliver_horn@qq.com"

COPY .docker-compose/nginx/conf.d/my.conf /etc/nginx/conf.d/my.conf
COPY --from=0 /gva_web/dist /usr/share/nginx/html
COPY --from=0 /gva_web/dist /usr/share/nginx/html/admin/
RUN cat /etc/nginx/nginx.conf
RUN cat /etc/nginx/conf.d/my.conf
RUN ls -al /usr/share/nginx/html

