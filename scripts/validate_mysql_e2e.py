from __future__ import annotations

import json
import os
import subprocess
import sys
import time


MYSQL_CONTAINER = os.getenv("MYSQL_CONTAINER", "mysql8")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "service_hub_e2e")
HUB_CONTAINER = os.getenv("HUB_CONTAINER", "service-hub-v2-mysql-e2e")
AGENT_CONTAINER = os.getenv("AGENT_CONTAINER", "service-agent-v2-mysql-e2e")
TARGET_CONTAINER = os.getenv("TARGET_CONTAINER", "orchidea-v2-mysql-host-nginx")
TEST_ROOT = os.getenv("TEST_ROOT", "/tmp/orchidea-v2-mysql-host")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "local-test-admin-token")
AGENT_ID = os.getenv("AGENT_ID", "v2-mysql-agent")
WS_URL = os.getenv("WS_URL", "ws://127.0.0.1:8080/ws/agent")


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", check=check)


def wsl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run("wsl.exe", *args, check=check)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return wsl("docker", *args, check=check)


def bash(script: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return wsl("bash", "-lc", script, check=check)


def docker_exec_python(container: str, source: str) -> str:
    result = docker("exec", container, "python", "-c", source, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "docker exec python failed\n"
            f"container={container}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def hub_request(path: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: dict | None = None) -> dict | list:
    headers_json = json.dumps(headers or {}, ensure_ascii=False)
    body_json = json.dumps(body, ensure_ascii=False) if body is not None else None
    source = [
        "import json, urllib.request",
        f"headers = json.loads({headers_json!r})",
    ]
    if body_json is None:
        source.append("data = None")
    else:
        source.extend(
            [
                f"payload = json.loads({body_json!r})",
                "data = json.dumps(payload).encode('utf-8')",
            ]
        )
    source.extend(
        [
            f"req = urllib.request.Request('http://127.0.0.1:8080{path}', data=data, headers=headers, method={method!r})",
            "print(urllib.request.urlopen(req, timeout=10).read().decode())",
        ]
    )
    return json.loads(docker_exec_python(HUB_CONTAINER, "; ".join(source)))


def resolve_mysql_password() -> str:
    env_password = os.getenv("MYSQL_ROOT_PASSWORD") or os.getenv("MYSQL_PASSWORD")
    if env_password:
        return env_password

    result = docker("inspect", MYSQL_CONTAINER, "--format", "{{range .Config.Env}}{{println .}}{{end}}", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to inspect MySQL container '{MYSQL_CONTAINER}': {result.stderr.strip()}")

    for line in result.stdout.splitlines():
        if line.startswith("MYSQL_ROOT_PASSWORD="):
            return line.split("=", 1)[1]
        if line.startswith("MARIADB_ROOT_PASSWORD="):
            return line.split("=", 1)[1]

    raise RuntimeError(
        "MySQL root password could not be resolved. Set MYSQL_ROOT_PASSWORD in the environment or expose it on the running container."
    )


def cleanup() -> None:
    bash(
        (
            f"docker rm -f {AGENT_CONTAINER} {HUB_CONTAINER} {TARGET_CONTAINER} >/dev/null 2>&1 || true; "
            f"docker compose -f {TEST_ROOT}/managed/e2e-app/docker-compose.yml down >/dev/null 2>&1 || true; "
            f"rm -rf {TEST_ROOT}"
        ),
        check=False,
    )


def prepare_database(mysql_password: str) -> None:
    docker(
        "exec",
        MYSQL_CONTAINER,
        "mysql",
        "-uroot",
        f"-p{mysql_password}",
        "-e",
        (
            f"DROP DATABASE IF EXISTS {MYSQL_DATABASE}; "
            f"CREATE DATABASE {MYSQL_DATABASE} CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;"
        ),
    )


def prepare_target_compose() -> None:
    bash(
        f"mkdir -p {TEST_ROOT}/managed/e2e-app && cat > {TEST_ROOT}/managed/e2e-app/docker-compose.yml <<'EOF'\n"
        "services:\n"
        "  app:\n"
        "    image: nginx:1.27-alpine\n"
        f"    container_name: {TARGET_CONTAINER}\n"
        "EOF"
    )


def build_images() -> None:
    hub_root = "/mnt/c/Users/bigno/Documents/work/orchisky/src/orchidea/service-hub"
    agent_root = "/mnt/c/Users/bigno/Documents/work/orchisky/src/orchidea/service-agent"
    bash(
        f"cd {hub_root} && docker build -t service-hub:v2-mysql-e2e . >/tmp/service-hub-v2-mysql-e2e.log && "
        f"cd {agent_root} && docker build -t service-agent:v2-mysql-e2e . >/tmp/service-agent-v2-mysql-e2e.log"
    )


def start_hub(mysql_password: str) -> None:
    docker(
        "run",
        "-d",
        "--name",
        HUB_CONTAINER,
        "--network",
        f"container:{MYSQL_CONTAINER}",
        "-e",
        f"ADMIN_TOKEN={ADMIN_TOKEN}",
        "-e",
        "PORT=8080",
        "-e",
        f"DATABASE_URL=mysql+pymysql://root:{mysql_password}@127.0.0.1:3306/{MYSQL_DATABASE}",
        "service-hub:v2-mysql-e2e",
    )


def provision_agent() -> str:
    for _ in range(30):
        try:
            response = hub_request(
                "/api/agents",
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Admin-Token": ADMIN_TOKEN,
                },
                body={"agentId": AGENT_ID},
            )
            return response["agentKey"]
        except Exception:
            time.sleep(1)

    raise RuntimeError("Failed to provision agent through service-hub")


def start_agent(agent_key: str) -> None:
    docker(
        "run",
        "-d",
        "--name",
        AGENT_CONTAINER,
        "--network",
        f"container:{MYSQL_CONTAINER}",
        "-e",
        f"WS_URL={WS_URL}",
        "-e",
        f"AGENT_ID={AGENT_ID}",
        "-e",
        f"AGENT_KEY={agent_key}",
        "-e",
        "RECONNECT_DELAY=2",
        "-e",
        "HEARTBEAT_INTERVAL=5",
        "-e",
        "HEALTH_PORT=18081",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{TEST_ROOT}/managed:/data",
        "service-agent:v2-mysql-e2e",
    )


def start_environment(mysql_password: str) -> None:
    cleanup()
    prepare_database(mysql_password)
    prepare_target_compose()
    build_images()
    bash(f"docker compose -f {TEST_ROOT}/managed/e2e-app/docker-compose.yml up -d >/dev/null")
    start_hub(mysql_password)
    agent_key = provision_agent()
    start_agent(agent_key)
    time.sleep(12)


def wait_for_command(request_id: str) -> dict:
    for _ in range(30):
        status = json.loads(
            docker_exec_python(
                HUB_CONTAINER,
                (
                    "import urllib.request; "
                    f"print(urllib.request.urlopen('http://127.0.0.1:8080/api/commands/{request_id}', timeout=10).read().decode())"
                ),
            )
        )
        if status["status"] not in {"queued", "processing"}:
            return status
        time.sleep(2)
    return status


def restart_hub(mysql_password: str) -> None:
    docker("rm", "-f", HUB_CONTAINER)
    start_hub(mysql_password)
    time.sleep(8)


def main() -> int:
    mysql_password = resolve_mysql_password()
    validation_completed = False
    try:
        start_environment(mysql_password)

        dispatch = hub_request(
            f"/api/agents/{AGENT_ID}/commands",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Requested-By": "copilot-e2e",
                "X-Requested-Source": "mysql-validation",
            },
            body={"action": "restart", "dir": "/data/e2e-app"},
        )
        request_id = dispatch["command"]["requestId"]
        final_status = wait_for_command(request_id)
        events_before = hub_request(f"/api/commands/{request_id}/events")

        if dispatch["command"]["requestedBy"] != "copilot-e2e":
            raise RuntimeError("requestedBy was not persisted")
        if dispatch["command"]["requestSource"] != "mysql-validation":
            raise RuntimeError("requestSource was not persisted")
        if final_status["status"] != "success":
            raise RuntimeError(f"command did not succeed: {final_status}")
        if [event["eventType"] for event in events_before] != ["created", "ack", "result"]:
            raise RuntimeError(f"unexpected event sequence: {events_before}")

        restart_hub(mysql_password)

        persisted_status = hub_request(f"/api/commands/{request_id}")
        persisted_events = hub_request(f"/api/commands/{request_id}/events")
        agents_after = hub_request("/api/agents")

        if persisted_status["requestedBy"] != "copilot-e2e":
            raise RuntimeError("requestedBy was not preserved after hub restart")
        if persisted_status["requestSource"] != "mysql-validation":
            raise RuntimeError("requestSource was not preserved after hub restart")
        if not any(agent["agentId"] == AGENT_ID and agent["credentialConfigured"] for agent in agents_after):
            raise RuntimeError("agent credentials were not preserved after hub restart")

        summary = {
            "dispatch": dispatch,
            "final_status_before_restart": final_status,
            "events_before_restart": events_before,
            "persisted_status_after_restart": persisted_status,
            "persisted_events_after_restart": persisted_events,
            "agents_after_restart": agents_after,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        validation_completed = True
        return 0
    finally:
        hub_result = docker("logs", "--tail", "30", HUB_CONTAINER, check=False)
        agent_result = docker("logs", "--tail", "30", AGENT_CONTAINER, check=False)
        hub_logs = (hub_result.stdout + hub_result.stderr).strip()
        agent_logs = (agent_result.stdout + agent_result.stderr).strip()
        if hub_logs:
            print("\n=== hub logs ===")
            print(hub_logs)
        if agent_logs:
            print("\n=== agent logs ===")
            print(agent_logs)
        if validation_completed and agent_logs:
            if "Using 'docker compose' (v2 plugin)." not in agent_logs:
                raise RuntimeError("service-agent did not use docker compose v2 during MySQL E2E validation")
            if "Using 'docker-compose' (v1 standalone)." in agent_logs:
                raise RuntimeError("service-agent unexpectedly fell back to docker-compose v1 during MySQL E2E validation")
        cleanup()


if __name__ == "__main__":
    sys.exit(main())