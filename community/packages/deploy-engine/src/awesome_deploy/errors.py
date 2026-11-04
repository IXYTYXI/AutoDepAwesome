class DeployError(Exception):
    def __init__(self, code: str, message: str, path: str = "", suggestion: str = "", *, reason: str = ""):
        super().__init__(message)
        self.code, self.message, self.path, self.suggestion = code, message, path, suggestion
        self.reason = reason

    def as_dict(self):
        data = {"code": self.code, "message": self.message, "path": self.path, "suggestion": self.suggestion}
        if self.reason:
            data["reason"] = self.reason
        return data
