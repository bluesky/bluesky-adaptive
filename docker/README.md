# Docker Notes

The following docker files are split up for the queue-server and http-server for testing. The docker compose here will spin them up together along with a networked redis server. They are bare bones and will lack the full functionality of a collection profile, but should be useful for simple testing.

## To run the containers:
On a Mac, to use the display you will need to install XQuartz and allow connections from network clients.
The bash script `local_display.sh` will set up a LOCAL_DISPLAY variable of the host machine and add it to the XQuartz access control list (if needed).
The queue-monitor gui image is built on [jozo/pyqt5](https://hub.docker.com/r/jozo/pyqt5).

```bash
cd docker/queue-server
docker build -t qserver:latest .
# The http-server image is based on the queue-server image so goes second.
cd ../http-server
docker build -t http-server:latest .
cd ../queue-monitor
docker build -t queue-monitor:latest .
source local_display.sh
cd ../
docker-compose up
```
