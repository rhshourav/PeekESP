"""
peek_services.py - everything the agent runs besides the sampler and the push.

One object, used the same way by the tray app and the headless agent:

    svc = Services(cfg, lambda: snapshot(messages=False), host)
    svc.start()
    ... snapshot() asks svc.extras_payload() and svc.outbox.active() ...
    svc.configure(new_cfg); svc.restart()

It owns the outbox (and so survives a settings save with its messages intact),
the extra sensors, the rule engine, the watched folder and the local endpoint.
"""

import peek_config
from peek_alerts import AlertEngine, flatten
from peek_messages import NotifyServer, Outbox, OutboxWatcher
from peek_sensors import ExtraSensors


class Services:
    def __init__(self, cfg, snapshot_fn, host, state_dir=None):
        self.cfg = cfg
        self.snapshot_fn = snapshot_fn
        self.host = host
        self.dir = state_dir or peek_config.config_dir()
        self.outbox = Outbox(self.dir / "outbox.json", cfg["message_ttl_hours"], host)
        self.engine = AlertEngine(lambda: flatten(self.snapshot_fn()), self.outbox,
                                  cfg["alerts"], host,
                                  history_path=self.dir / "history.json")
        self.extras = self._new_extras()
        self.watcher = None
        self.server = None
        self.running = False

    @property
    def outbox_dir(self):
        return self.dir / "outbox"

    def _new_extras(self):
        return ExtraSensors(self.cfg["sensors_enabled"], self.cfg["extras_interval"],
                            self.cfg["sensors"])

    # -- what a reading asks for -------------------------------------------
    def extras_payload(self):
        return self.extras.payload()

    # -- lifecycle ---------------------------------------------------------
    def configure(self, cfg):
        self.cfg = cfg
        self.outbox.configure(ttl_hours=cfg["message_ttl_hours"], host=self.host)
        self.engine.configure(cfg["alerts"])

    def start(self):
        """Start (or restart) the background parts. Safe to call repeatedly."""
        self.stop()
        cfg = self.cfg
        self.extras = self._new_extras()
        self.extras.start()
        self.engine.start()

        self.watcher = OutboxWatcher(self.outbox, self.outbox_dir)
        self.watcher.start()

        self.server = None
        if cfg["notify_enabled"]:
            if not cfg["notify_key"]:
                # Generated once and kept: a key that changed on every start
                # would break every program that had been told it.
                cfg["notify_key"] = peek_config.new_token()
                try:
                    peek_config.save(cfg)
                except OSError:
                    pass
            srv = NotifyServer(self.outbox, cfg["notify_key"], cfg["notify_port"])
            self.server = srv
            srv.start()
        self.running = True

    def stop(self):
        for part in (self.extras, self.engine, self.watcher, self.server):
            if part is not None:
                part.stop()
        self.running = False

    # -- for status lines --------------------------------------------------
    def problems(self):
        out = []
        if self.server is not None and self.server.error:
            out.append("local endpoint: " + self.server.error)
        if self.engine.last_error:
            out.append("alert rules: " + self.engine.last_error)
        if self.outbox.save_error:
            out.append("outbox: " + self.outbox.save_error)
        return out