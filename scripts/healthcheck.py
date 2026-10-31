from urllib.request import urlopen

with urlopen("http://127.0.0.1:8000/health", timeout=5) as response:
    assert response.status == 200
    print(response.read().decode())
