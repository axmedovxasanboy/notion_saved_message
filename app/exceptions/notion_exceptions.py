

class NotionPageIdNotSpecified(Exception):
    def __init__(self, message, error_code, function_name):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.function_name = function_name

    def __str__(self):
        return f"{self.message} Occurred in [{self.function_name}] (Error Code: {self.error_code})"

    def format_error(self) -> str:
        return f"{self.__str__()}"



