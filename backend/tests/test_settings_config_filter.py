"""
Test del fix "settings bloat": get_config_settings() esclude lo stato
transitorio namespaced ("::"), get_all_settings() lo include ancora, e
purge_transient_settings() rimuove solo i prefissi effimeri di default.

Gira sul backend SQLite (conftest forza DB_PATH temporaneo).
"""
import database


def test_config_excludes_transient_keys():
    # config reali
    database.set_setting("crypto_decision_engine", "core_bound")
    database.set_setting("user_risk_profile", "moderate")
    database.set_setting("prompt_decision_crypto", "x")
    # stato transitorio (namespaced con ::)
    database.set_setting("_chat_fallback::conv::123", '{"a":1}')
    database.set_setting("_sim_run_progress::abc-uuid", '{"step":3}')
    database.set_setting("_sim_scenario::dynamic_index", "[]")

    cfg = database.get_config_settings()
    # le config ci sono
    assert cfg.get("crypto_decision_engine") == "core_bound"
    assert cfg.get("user_risk_profile") == "moderate"
    assert "prompt_decision_crypto" in cfg
    # la spazzatura NO
    assert not any("::" in k for k in cfg), f"chiavi transitorie trapelate: {[k for k in cfg if '::' in k]}"
    assert "_chat_fallback::conv::123" not in cfg
    assert "_sim_run_progress::abc-uuid" not in cfg


def test_all_settings_still_includes_transient():
    # get_all_settings DEVE ancora vedere tutto (chat-fallback ci si appoggia)
    database.set_setting("_chat_fallback::conv::777", '{"keep":true}')
    database.set_setting("real_key", "v")
    allk = database.get_all_settings()
    assert "_chat_fallback::conv::777" in allk
    assert allk.get("real_key") == "v"


def test_crypto_decision_engine_visible_even_with_bloat():
    # simula il bloat: >1000 chiavi transitorie + il flag reale
    for i in range(1100):
        database.set_setting(f"_sim_run_progress::r{i}", "{}")
    database.set_setting("crypto_decision_engine", "core_bound")
    cfg = database.get_config_settings()
    # il flag NON deve sparire dietro la massa di chiavi transitorie
    assert cfg.get("crypto_decision_engine") == "core_bound"
    # e la view config resta piccola (niente transitorie)
    assert len(cfg) < 100


def test_purge_default_only_sim_progress_and_fallback():
    # Prefissi unici a questo test per isolarlo dallo stato di altri test
    # (il modulo `database` e il DB SQLite sono condivisi nella sessione).
    P = "_sim_run_progress::purgetest_"
    F = "_sim_run_fallback::purgetest_"
    database.set_setting(P + "a", "{}")
    database.set_setting(P + "b", "{}")
    database.set_setting(F + "list", "[]")
    database.set_setting("_chat_fallback::conv::purgetest", "{}")  # NON deve sparire
    database.set_setting("crypto_decision_engine", "core_bound")    # NON deve sparire

    # dry-run conta soltanto (sui prefissi unici di questo test)
    rep = database.purge_transient_settings(prefixes=(P, F), dry_run=True)
    assert rep.get(P) == 2
    assert rep.get(F) == 1
    allk = database.get_all_settings()
    assert (P + "a") in allk  # dry-run non ha cancellato

    # purge reale
    database.purge_transient_settings(prefixes=(P, F))
    allk2 = database.get_all_settings()
    assert (P + "a") not in allk2
    assert (F + "list") not in allk2
    # i non-target restano intatti
    assert "_chat_fallback::conv::purgetest" in allk2
    assert allk2.get("crypto_decision_engine") == "core_bound"


def test_purge_default_prefixes_spare_chat_and_config():
    # Verifica il DEFAULT (senza prefixes espliciti): tocca solo sim progress/
    # fallback, mai chat-fallback ne' config.
    database.set_setting("_chat_fallback::conv::keepme", "{}")
    database.set_setting("user_risk_profile", "moderate")
    database.purge_transient_settings()  # default prefixes
    allk = database.get_all_settings()
    assert "_chat_fallback::conv::keepme" in allk
    assert allk.get("user_risk_profile") == "moderate"


def test_config_filter_behavior_via_public_api():
    # Verifica il discriminatore "::" attraverso l'API pubblica (il helper
    # _is_config_key non e' ri-esportato da `import *`, ma il comportamento si').
    database.set_setting("plain_config_key", "v")
    database.set_setting("ns::transient_key", "v")
    cfg = database.get_config_settings()
    assert "plain_config_key" in cfg
    assert "ns::transient_key" not in cfg
