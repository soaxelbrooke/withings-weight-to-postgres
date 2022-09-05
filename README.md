
# withings-weight-to-postgres

Pulls your weight continuously, writing it to a database.

```bash
# For first run, or if credentials expire, you need to run it interactively:
$ docker run -it --env-file .env withings-weight-to-postgres
WARNING:2022-09-05,16:02:07.715 [MainThread] - Withings authorization failed, attempting to refresh...


Go to the following URL, click the link, and paste the resulting `code` parameter:
...SOME_URL

URL Code:
```

Once you authenticate once, you can cancel the execution and run the container normally. After this point, token refresh should be handled automatically.

## Configuration

Provide the following environment variables to configure:

```
WITHINGS_CLIENT_ID=foo
WITHINGS_SECRET=bar
SQLALCHEMY_CONN_STRING=baz
REFRESH_PERIOD="5 minutes"
```

It will use `dev.null.test` as the callback uri. You can also set `LOG_LEVEL` if you want more or less verbosity.
