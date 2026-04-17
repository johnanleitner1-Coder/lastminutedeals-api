# BEFORE:
        for attempt in range(2 if retry_on_timeout else 1):
            ...
            except requests.exceptions.Timeout:
                if attempt == 0 and retry_on_timeout:

# AFTER:
        for attempt in range(2):  # always retry once on timeout (Bokun /products is slow)
            ...
            except requests.exceptions.Timeout:
                if attempt == 0:
