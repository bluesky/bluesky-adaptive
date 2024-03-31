# Agents as a service
Running asynchronous or distributed agents behind a Uvicorn FastAPI server in the context of bluesky-adaptive introduces a powerful, asynchronous approach to controlling and orchestrating experiments.
This setup leverages the concurrent handling of HTTP requests provided by Uvicorn, an ASGI server, in combination with the straightforward, yet robust, API creation capabilities of FastAPI.
Together, they enable the development of responsive, efficient, and easily accessible web services for managing adaptive agents in experimental workflows.

## Overview
[FastAPI](https://fastapi.tiangolo.com) is a modern, high-performance web framework for building APIs with Python 3.7+ based on standard Python type hints.
Its simplicity and performance make it ideal for creating web services for scientific applications, such as managing autonomous agents in a Bluesky-adaptive setup.
In the context of Bluesky-adaptive, deploying agents behind a Uvicorn FastAPI server enables the agents to be managed via web API calls, facilitating integration with other systems and providing a user-friendly interface for monitoring and controlling the agents. This setup allows for:

- **Dynamic Agent Management**: Agents can be started, stopped, and configured on-the-fly through HTTP requests, providing flexibility in experiment orchestration.
- **Asynchronous Operation**: Both Uvicorn and FastAPI are designed with asynchronous operation in mind, allowing for non-blocking IO operations. This is particularly beneficial for agents that need to perform long-running tasks, such as data analysis or waiting for new data, without freezing the server.
- **Real-time Monitoring and Control**: The web service can offer endpoints for monitoring the state and progress of agents and experiments. This real-time feedback loop is invaluable for adapting strategies on-the-go and for human intervention when necessary.
- **Scalability**: With Uvicorn and FastAPI, scaling the number of agents or adapting the system to handle more complex experiments becomes straightforward, thanks to their lightweight, resource-efficient nature.
