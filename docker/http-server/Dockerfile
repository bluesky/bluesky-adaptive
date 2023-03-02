# docker build -t http-server:latest .
FROM  qserver:latest

RUN pip3 install bluesky-httpserver

ENV QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY="SECRET"
#ENV QSERVER_HTTP_SERVER_ALLOW_ANONYMOUS_ACCESS=1
ENV QSERVER_ZMQ_CONTROL_ADDRESS=tcp://qserver:60615
ENV QSERVER_ZMQ_INFO_ADDRESS=tcp://qserver:60625
EXPOSE 60610

CMD uvicorn bluesky_httpserver.server:app --host 0.0.0.0 --port 60610
