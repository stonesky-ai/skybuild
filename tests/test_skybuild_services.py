"""Check that fleet control cannot restart or stop an unverified service."""
import json
from unittest.mock import patch
import pytest
from scripts import skybuild_services as services


@pytest.fixture
def container():
    return dict(kind="container", host="wonko", name="skybuild-test", id="a"*64,
                image="sha256:"+"b"*64, project="skybuild-pilot", service="api",
                memory_max_bytes=512*1024**2)


def inspected(item, *, running=True):
    return json.dumps([dict(Id=item["id"], Image=item["image"], Name="/"+item["name"],
        Config=dict(Labels={"com.docker.compose.project":"skybuild-pilot",
                            "com.docker.compose.service":"api"}),
        HostConfig=dict(Memory=item["memory_max_bytes"]), State=dict(Running=running))])


def test_ensure_running_does_not_restart(container):
    calls=[]
    def run(argv, **kwargs):
        calls.append(argv)
        return "wonko" if argv[1]=="info" else inspected(container)
    with patch.object(services.socket,"gethostname",return_value="wonko"), patch.object(services,"command",side_effect=run):
        assert services.local(container,"ensure-running")["ok"]
    assert all("start" not in c and "stop" not in c for c in calls)


def test_stop_refuses_replaced_container(container):
    altered={**container,"id":"c"*64}
    with patch.object(services.socket,"gethostname",return_value="wonko"), patch.object(services,"command",side_effect=["wonko",inspected(altered)]):
        with pytest.raises(services.ServiceError,match="identity"):
            services.local(container,"stop")


def test_stop_refuses_remote_docker(container):
    with patch.object(services.socket,"gethostname",return_value="wonko"), patch.object(services,"command",return_value="jeltz"):
        with pytest.raises(services.ServiceError,match="daemon"):
            services.local(container,"stop")


def test_transient_unit_is_not_replayed():
    item=dict(kind="unit",host="wonko",name="skybuild-watch.service",fragment="/run/user/1000/systemd/transient/skybuild-watch.service")
    data="Id=skybuild-watch.service\nFragmentPath="+item["fragment"]+"\nTransient=yes\nActiveState=inactive\nSubState=dead"
    with patch.object(services.socket,"gethostname",return_value="wonko"), patch.object(services.Path,"exists",return_value=True), patch.object(services,"command",return_value=data):
        result = services.local(item,"ensure-running")
        assert result["skipped"] and result["observation_only"] and not result["ok"]


def test_host_failure_does_not_hide_other_hosts(tmp_path,capsys):
    items=[dict(kind="capability",host=h,name="skybuild-worker",checkout="/missing",head="a"*40) for h in ["wonko","wowbagger"]]
    inventory=tmp_path/"inventory.json";inventory.write_text(json.dumps(dict(schema="skybuild.services.v1",services=items)))
    with patch.object(services,"operate",side_effect=[services.ServiceError("Host unavailable"),dict(state="available",ok=True)]):
        assert services.main(["--inventory",str(inventory)])==1
    result=json.loads(capsys.readouterr().out)
    assert len(result["services"])==2
    assert result["services"][1]["ok"]


def test_invalid_host_cannot_be_an_ssh_option(container):
    with pytest.raises(services.ServiceError,match="host"):
        services.validate({**container,"host":"-F"})


def test_start_blocks_dependent_services(tmp_path,capsys):
    items=[dict(kind="capability",host="wonko",name="skybuild-"+n,checkout="/missing",head="a"*40) for n in ["one","two"]]
    inventory=tmp_path/"inventory.json";inventory.write_text(json.dumps(dict(schema="skybuild.services.v1",services=items)))
    with patch.object(services,"operate",return_value=dict(state="missing",ok=False)) as operate:
        assert services.main(["ensure-running","--inventory",str(inventory)])==1
        assert operate.call_count==1


def test_stop_accepts_inactive_exited_unit():
    item=dict(kind="unit",host="wonko",name="skybuild-watch.service",fragment="/home/kevin/.config/systemd/user/skybuild-watch.service")
    data="Id=skybuild-watch.service\nFragmentPath="+item["fragment"]+"\nTransient=no\nActiveState=inactive\nSubState=exited"
    with patch.object(services.socket,"gethostname",return_value="wonko"), patch.object(services.Path,"exists",return_value=True), patch.object(services,"command",side_effect=[data,"",data,"inactive"]):
        result=services.local(item,"stop")
        assert result["ok"] and result["desired_state"] == "stopped"


def test_persistent_oneshot_can_be_healthy():
    item=dict(kind="unit",host="wonko",name="skybuild-watch.service",fragment="/home/kevin/.config/systemd/user/skybuild-watch.service",oneshot=True)
    data="Id=skybuild-watch.service\nFragmentPath="+item["fragment"]+"\nTransient=no\nActiveState=active\nSubState=exited"
    with patch.object(services.socket,"gethostname",return_value="wonko"), patch.object(services.Path,"exists",return_value=True), patch.object(services,"command",return_value=data):
        assert services.local(item,"status")["ok"]


def test_start_waits_for_health_without_restarting(container):
    starting=json.loads(inspected(container));starting[0]["State"]["Health"]={"Status":"starting"}
    healthy=json.loads(inspected(container));healthy[0]["State"]["Health"]={"Status":"healthy"}
    replies=["wonko",inspected(container,running=False),"", "wonko",json.dumps(starting),"wonko",json.dumps(healthy)]
    with patch.object(services.socket,"gethostname",return_value="wonko"), patch.object(services,"command",side_effect=replies) as command, patch.object(services.Path,"read_text",return_value="MemAvailable: 32000000 kB"), patch.object(services.time,"sleep"), patch.object(services,"check_watch"):
        result=services.local(container,"start")
        assert result["ok"]
        assert sum(call.args[0][1]=="start" for call in command.call_args_list)==1


def test_missing_watch_blocks_start(container):
    with patch.object(services.socket,"gethostname",return_value="wonko"), patch.object(services,"command",side_effect=["wonko",inspected(container,running=False)]) as command:
        with pytest.raises(services.ServiceError,match="watch"):
            services.local(container,"start")
        assert command.call_count==2


def test_stop_is_idempotent_for_an_initially_stopped_container(container):
    with patch.object(services.socket,"gethostname",return_value="wonko"), patch.object(services,"command",side_effect=["wonko",inspected(container,running=False)]) as command:
        result = services.local(container,"stop")
        assert result["ok"] and result["desired_state"] == "stopped"
        assert command.call_count == 2


def test_stop_skips_an_inactive_observation_without_failure():
    item=dict(kind="unit",host="wonko",name="skybuild-watch.service",fragment="/run/user/1000/systemd/transient/skybuild-watch.service")
    data="Id=skybuild-watch.service\nFragmentPath="+item["fragment"]+"\nTransient=yes\nActiveState=inactive\nSubState=dead"
    with patch.object(services.socket,"gethostname",return_value="wonko"), patch.object(services.Path,"exists",return_value=True), patch.object(services,"command",return_value=data) as command:
        result = services.local(item,"stop")
        assert result["skipped"] and result["ok"]
        assert command.call_count == 1
