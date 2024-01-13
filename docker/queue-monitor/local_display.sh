#!/bin/bash

# Set up local display and xhost access on Mac or Linux
# Check if the operating system is Mac or Linux
if [[ "$(uname)" == "Darwin" ]]; then
    # On a Mac, set LOCAL_DISPLAY_IP to the IP address
    LOCAL_DISPLAY_IP=$(ifconfig en0 | grep inet | awk '$1=="inet" {print $2}')
elif [[ "$(uname)" == "Linux" ]]; then
    # On Linux, set LOCAL_DISPLAY_IP to an empty string
    LOCAL_DISPLAY_IP=""
fi
# Set LOCAL_DISPLAY
if [[ -n "$LOCAL_DISPLAY_IP" ]]; then
    LOCAL_DISPLAY="${LOCAL_DISPLAY_IP}:0"
else
    LOCAL_DISPLAY="$DISPLAY"
fi
# If on a Mac, execute xhost + with LOCAL_DISPLAY_IP
if [[ "$(uname)" == "Darwin" ]]; then
    xhost +"$LOCAL_DISPLAY_IP"
fi
export LOCAL_DISPLAY_IP LOCAL_DISPLAY
