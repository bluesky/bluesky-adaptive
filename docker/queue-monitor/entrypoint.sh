#!/bin/bash

# echo "Starting VNC server..."
# # Setup VNC server
# Xvfb :1 -screen 0 2560x1440x16 &
# x11vnc -rfbport 5901 -bg -quiet -forever -shared -display :1

echo "Starting queue-monitor GUI..."
echo "Control address: $QSERVER_ZMQ_CONTROL_ADDRESS"
echo "Publish address: $QSERVER_ZMQ_INFO_ADDRESS"
queue-monitor