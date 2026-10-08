from stockmate import analysis


def test_claude_env_drops_broker_keys(monkeypatch):
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "s")
    monkeypatch.setenv("KIS_APP_SECRET", "k")
    monkeypatch.setenv("WEB_PASSWORD", "p")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "keep")
    env = analysis.claude_env()
    assert not {"TOSS_CLIENT_SECRET", "KIS_APP_SECRET", "WEB_PASSWORD", "CLAUDECODE"} & env.keys()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "keep"  # 구독 로그인은 남긴다
