FROM jozo/pyqt5

# Upgrade pip and setuptools
RUN apt-get update \
    && apt-get install -y \
    python3-pip \
    python3-setuptools \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --upgrade pip setuptools

# Install bluesky-widgets and its dependencies
RUN pip3 install bluesky-queueserver bluesky-queueserver-api
RUN pip3 install 'bluesky-widgets[complete]'

ENV QSERVER_ZMQ_CONTROL_ADDRESS=tcp://127.0.0.1:60615
ENV QSERVER_ZMQ_INFO_ADDRESS=tcp://127.0.0.1:60625

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# VNC viewer
EXPOSE 5901

# Sets up and echos the environment
ENTRYPOINT [ "/entrypoint.sh" ]

CMD ["queue-monitor"]
