import uuid


def test_register_and_login_volunteer(client):
    email = f"test-{uuid.uuid4()}@example.com"

    register_resp = client.post(
        "/auth/register/volunteer",
        json={
            "email": email,
            "password": "supersecret123",
            "full_name": "Test Volunteer",
            "is_over_14": True,
        },
    )
    assert register_resp.status_code == 201

    login_resp = client.post(
        "/auth/login",
        data={"username": email, "password": "supersecret123"},
    )
    assert login_resp.status_code == 200
    assert "access_token" in login_resp.json()


def test_register_volunteer_under_14_rejected(client):
    email = f"test-{uuid.uuid4()}@example.com"

    register_resp = client.post(
        "/auth/register/volunteer",
        json={
            "email": email,
            "password": "supersecret123",
            "full_name": "Too Young",
            "is_over_14": False,
        },
    )
    assert register_resp.status_code == 400
