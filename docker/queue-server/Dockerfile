# docker build -t qserver:latest .
FROM  fedora
# Lock in a python version because of some backward compatability issues
RUN dnf update -y \
    && dnf install -y \
    python3.9 \
    g++ \
    gcc \
    git \
    && dnf clean all

RUN python3.9 -m ensurepip --upgrade


RUN pip3 install bluesky-queueserver

# Add custom plans
COPY 16-plans.py /usr/local/lib/python3.9/site-packages/bluesky_queueserver/profile_collection_sim/

ENV REDIS_ADDR=redis:6379
ENV QSERVER_ZMQ_CONTROL_ADDRESS=tcp://127.0.0.1:60615
ENV QSERVER_ZMQ_INFO_ADDRESS=tcp://127.0.0.1:60625

CMD start-re-manager --redis-addr ${REDIS_ADDR}
