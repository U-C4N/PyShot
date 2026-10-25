class PyShotError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}
