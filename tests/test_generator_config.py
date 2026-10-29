"""Real default generators use explicit configuration and mocked HTTP only."""
import io
import json
from types import SimpleNamespace

import pytest

from bagu_trainer import bootstrap
from bagu_trainer.application.learning import engine as legacy
from bagu_trainer.application.learning import generator as qg
from bagu_trainer.infrastructure import config, secrets
from tests.test_quiz_generator import UNVERIFIED, VALID_LLM_CONTENT, VALID_REVIEW


CONTEXT = "为订单查询增加读写分离与主从复制"


def model(name, timeout):
    return {"base_url": f"https://{name}.invalid/v1", "model": name,
            "api_key": f"synthetic-{name}-credential", "timeout_sec": timeout}


@pytest.fixture
def model_http(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    (profile / "data").mkdir(parents=True)
    (profile / "data" / "questions.json").write_text(
        json.dumps([dict(UNVERIFIED, tags=[], keywords=[])]), encoding="utf-8")
    monkeypatch.setenv("BAGU_USER_DATA_DIR", str(profile))
    monkeypatch.delenv(secrets.ENV_KEY, raising=False)
    monkeypatch.setattr(secrets, "_load_keyring", lambda: None)
    monkeypatch.setattr(config, "USER_DATA_ROOT", profile)
    monkeypatch.setattr(config, "CONFIG_PATH", profile / "config.json")
    config._write_config_atomic({"llm": model("global", 99)})
    state = SimpleNamespace(calls=[], clock=0.0, hook=None, instances=[])
    monkeypatch.setattr(qg, "time", SimpleNamespace(monotonic=lambda: state.clock))

    def opener(request, timeout=None):
        payload = json.loads(request.data)
        reviewing = "review_context_grounding" in payload["messages"][-1]["content"]
        state.calls.append({"url": request.full_url, "payload": payload,
                            "auth": request.get_header("Authorization"), "timeout": timeout})
        state.clock += 1
        if state.hook:
            state.hook(reviewing)
        content = json.dumps(VALID_REVIEW) if reviewing else VALID_LLM_CONTENT
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    monkeypatch.setattr(qg, "open_model_request", opener)

    def make(settings, **kwargs):
        cfg = {**config.DEFAULTS, "llm": settings,
               "project_roots": [str(tmp_path)], "db_path": str(tmp_path / "isolated.db")}
        instance = bootstrap.create_engine(cfg=cfg, **kwargs)
        state.instances.append(instance)
        assert type(instance.generator) is qg.QuizGenerator
        return instance

    state.make = make
    yield state
    for instance in state.instances:
        instance.close()


def assert_requests(calls, settings):
    assert len(calls) == 2
    assert [call["url"] for call in calls] == [settings["base_url"] + "/chat/completions"] * 2
    assert [call["payload"]["model"] for call in calls] == [settings["model"]] * 2
    assert [call["auth"] for call in calls] == ["Bearer " + settings["api_key"]] * 2
    assert [call["timeout"] for call in calls] == [settings["timeout_sec"], settings["timeout_sec"] - 1]


def test_two_default_generators_use_their_own_engine_configuration(model_http):
    first_config, second_config = model("first", 7), model("second", 13)
    first = model_http.make(first_config)
    second = model_http.make(second_config)
    for instance in (first, second):
        # Invoke outside Engine's compatibility context: configuration must be bound explicitly.
        result = instance.generator.generate(UNVERIFIED, CONTEXT)
        assert result["generated_by"] == "llm", result
    assert first.load_config()["llm"] == first_config
    assert second.load_config()["llm"] == second_config
    assert_requests(model_http.calls[:2], first_config)
    assert_requests(model_http.calls[2:], second_config)


def test_two_engine_loaders_override_only_their_own_base_configuration(model_http):
    first_config, second_config = model("loader-first", 9), model("loader-second", 17)
    reads = []

    def first_loader():
        reads.append("first")
        return {"llm": first_config, "context_char_limit": 1234}

    def second_loader():
        reads.append("second")
        return {"llm": second_config}

    first = model_http.make(model("base-first", 3), config_loader=first_loader)
    second = model_http.make(model("base-second", 5), config_loader=second_loader)
    for instance in (first, second):
        assert instance.generator.generate(UNVERIFIED, CONTEXT)["generated_by"] == "llm"
    assert reads == ["first", "second"]  # One config read for generation plus review.
    assert first.load_config()["context_char_limit"] == 1234
    assert first.load_config()["max_pending_quizzes"] == config.DEFAULTS["max_pending_quizzes"]
    assert second.load_config()["llm"] == second_config
    assert_requests(model_http.calls[:2], first_config)
    assert_requests(model_http.calls[2:], second_config)


@pytest.mark.parametrize("overrides", [None, {}, {"session_ingest": True}])
def test_loader_without_model_override_retains_instance_model(model_http, overrides):
    settings = model("base", 5)
    instance = model_http.make(settings, config_loader=lambda: overrides)
    assert instance.generator.generate(UNVERIFIED, CONTEXT)["generated_by"] == "llm"
    assert instance.load_config()["llm"] == settings
    assert_requests(model_http.calls, settings)


def test_explicit_model_removal_does_not_fall_back_to_global_config(model_http):
    instance = model_http.make(model("base", 5), config_loader=lambda: {"llm": None})
    assert instance.load_config()["llm"] is None
    assert instance.generator.generate(UNVERIFIED, CONTEXT)["generated_by"] == "unavailable"
    assert model_http.calls == []


def test_default_application_generator_reads_saved_updates_on_next_generation(model_http, monkeypatch):
    monkeypatch.setattr(bootstrap, "_engine", None)
    monkeypatch.setattr(legacy, "load_config", config.load_config)
    instance = bootstrap.get_engine()
    model_http.instances.append(instance)
    generator = instance.generator
    assert type(generator) is qg.QuizGenerator
    assert generator.generate(UNVERIFIED, CONTEXT)["generated_by"] == "llm"
    updated = model("saved", 12)
    config._write_config_atomic({"llm": updated})
    assert instance.load_config()["llm"] == updated
    assert bootstrap.get_engine() is instance
    assert instance.generator is generator
    assert generator.generate(UNVERIFIED, CONTEXT)["generated_by"] == "llm"
    assert_requests(model_http.calls[:2], model("global", 99))
    assert_requests(model_http.calls[2:], updated)


@pytest.mark.parametrize("source", ["cfg", "loader", "default_application"])
def test_configuration_change_during_generation_cannot_switch_review(model_http, monkeypatch, source):
    initial, updated = model("initial", 11), model("updated", 19)
    settings = dict(initial)
    if source == "default_application":
        config._write_config_atomic({"llm": settings})
        monkeypatch.setattr(bootstrap, "_engine", None)
        monkeypatch.setattr(legacy, "load_config", config.load_config)
        instance = bootstrap.get_engine()
        model_http.instances.append(instance)
    elif source == "loader":
        instance = model_http.make(model("unused-base", 3), config_loader=lambda: {"llm": settings})
    else:
        instance = model_http.make(settings)

    def change_config(reviewing):
        if not reviewing:
            # Mutate the same nested object, not just replace the outer config.
            settings.update(updated)
            config._write_config_atomic({"llm": settings})

    model_http.hook = change_config
    assert instance.generator.generate(UNVERIFIED, CONTEXT)["generated_by"] == "llm"
    assert_requests(model_http.calls, initial)
    model_http.hook = None
    assert instance.generator.generate(UNVERIFIED, CONTEXT)["generated_by"] == "llm"
    assert_requests(model_http.calls[2:], updated)


def test_injected_generator_is_preserved(model_http):
    supplied = object()
    instance = bootstrap.create_engine(cfg={"llm": model("provided", 5)}, generator=supplied)
    model_http.instances.append(instance)
    assert instance.generator is supplied
    assert model_http.calls == []


@pytest.mark.parametrize("legacy_config", [None, {"llm": model("ignored-legacy-argument", 3)}])
def test_standalone_generator_retains_dynamic_file_configuration(model_http, legacy_config):
    generator = qg.QuizGenerator(legacy_config)
    assert generator.generate(UNVERIFIED, CONTEXT)["generated_by"] == "llm"
    assert_requests(model_http.calls, model("global", 99))
    settings = model("standalone-saved", 8)
    config._write_config_atomic({"llm": settings})
    assert generator.generate(UNVERIFIED, CONTEXT)["generated_by"] == "llm"
    assert_requests(model_http.calls[2:], settings)
