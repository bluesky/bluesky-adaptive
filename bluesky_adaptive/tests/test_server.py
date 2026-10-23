def test_server_status(fastapi_client):
    """
    Test the server status endpoint.
    """
    response = fastapi_client.get("/api")
    assert response.status_code == 200
    assert response.json()["message"] == "The HTTP server is alive!!!"


def test_variables_names(fastapi_client):
    """
    Test the variables names endpoint.
    """
    response = fastapi_client.get("/api/variables/names")
    assert response.status_code == 200
    assert isinstance(response.json()["names"], list)
    assert len(response.json()["names"]) > 0  # Ensure there are some variable names returned


def test_variable_example(fastapi_client):
    """
    Test the variable 'depth' endpoint.
    """
    response = fastapi_client.get("/api/variable/depth")
    assert response.status_code == 200
    assert isinstance(response.json()["depth"], int)

    # Test setting the variable 'depth'
    new_depth = 42
    response = fastapi_client.post("/api/variable/depth", json={"value": new_depth})
    assert response.status_code == 200
    response = fastapi_client.get("/api/variable/depth")
    assert response.status_code == 200
    assert response.json()["depth"] == new_depth


def test_methods_all(fastapi_client):
    """
    Test the methods endpoint.
    """
    response = fastapi_client.get("/api/methods")
    assert response.status_code == 200
    assert isinstance(response.json(), dict)
    assert len(response.json()) > 0  # Ensure there are some methods returned
    assert "foo" in response.json()  # Check for a specific method


def test_method_example(fastapi_client):
    """
    Test the method 'foo' endpoint.
    """
    response = fastapi_client.get("/api/method/foo")
    assert response.status_code == 200
    response = response.json()
    assert "name" in response
    assert response["name"] == "foo"
    assert "description" in response
    assert "input_schema" in response
    assert "output_type" in response

    response = fastapi_client.post("/api/method/foo", json={"desc": "test"})
    assert response.status_code == 200

    # Test with invalid input (integer instead of string)
    response = fastapi_client.post("/api/method/foo", json={"desc": 5})
    assert response.status_code == 500
    assert "validation error" in response.json()["detail"]

    # Test with invalid keyword
    response = fastapi_client.post("/api/method/foo", json={"invalid_kwarg": "test"})
    assert response.status_code == 500
    assert "unexpected keyword argument" in response.json()["detail"]
