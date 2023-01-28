# Docker Notes

The following docker files are split up for the queue-server and http-server for testing. The docker compose here will spin them up together along with a networked redis server. They are bare bones and will lack the full functionality of a collection profile, but should be useful for simple testing.

## To run the containers:
```bash
cd docker/queue-server
docker build -t qserver:latest .
# The http-server image is based on the queue-server image so goes second.
cd ../http-server
docker build -t http-server:latest .
cd ../
docker-compose up
