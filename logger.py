class Logger:

    def __init__(self):
        self._reset()

    def _reset(self):
        self._base = ""
        self._targets = ""
        self._extra = ""

    def __setattr__(self, name, value):
        if name == "extra":
            if self._extra != "":
                self._extra += " | "
            self._extra += value
        else:
            super().__setattr__(f"_{name}", value)

    def __str__(self):
        """Returns log line and resets it"""
        result = (
            f"{self._base:52} |"
            f"{self._targets:40} |"
            f"{self._extra}"
        )
        self._reset()
        return result
