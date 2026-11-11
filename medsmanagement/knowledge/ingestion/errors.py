class IngestionError(Exception):
    def __init__(self, user_message):
        super().__init__(user_message)
        self.user_message = user_message
 
 
class UnsupportedFile(IngestionError):
    pass
 
 
class FileTooLarge(IngestionError):
    pass
 
 
class NoReadableText(IngestionError):
    pass
 
 
class InvalidURL(IngestionError):
    pass
 
 
class UnsafeURL(IngestionError):
    """Private or internal network address (localhost, 192.168.x.x, ...)."""
 
 
class PrivatePage(IngestionError):
    """The page needs a login, or the site said it is private."""
 
 
class BlockedByHost(IngestionError):
    """PythonAnywhere's free-tier allowlist blocked the site."""
 
 
class FetchFailed(IngestionError):
    pass