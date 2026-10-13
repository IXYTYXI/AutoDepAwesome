class DeployError(Exception):
    def __init__(self, code: str, message: str, path: str = "", suggestion: str = ""):
        super().__init__(message)
        self.code, self.message, self.path, self.suggestion = code, message, path, suggestion

    def as_dict(self):
        return {"code": self.code, "message": self.message, "path": self.path, "suggestion": self.suggestion}
