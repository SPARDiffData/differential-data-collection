"""The pilot store: a Google Cloud Storage bucket (STO-1, decision D3), behind `Store`.

The producer signs in once with their own Google account:
    gcloud auth application-default login
and the team's folder in the bucket gives them "Storage Object Creator" only: they can add
files there and nothing else. Every upload is create-only (`if_generation_match=0`), so the
bucket itself refuses to overwrite a file, even if our own checks were wrong (STO-3).

diffdata never reads, stores or prints the sign-in token: Google's library finds it.
Push imports this module only when a real push runs. Its own tests use a stand-in for
Google's client and sign-in, with no bucket, credentials or network.
"""

import google.auth
from google.api_core import exceptions as api
from google.auth import exceptions as auth
from google.auth.transport.requests import Request
from google.cloud import storage

from diffdata.store.backend import Added, SignInError, StoreError

SIGN_IN = (
    "Couldn't sign in to Google Cloud Storage. Run `gcloud auth application-default login` "
    "once, sign in with the Google account the project team gave access to, then run again. "
    "Nothing was sent."
)


# Google's own address, set here so no setting in the environment can send uploads elsewhere.
ENDPOINT = "https://storage.googleapis.com"
# Adding files to a bucket needs no Cloud project, and a producer's sign-in may not name one.
# This is the library's own placeholder (as in its create_anonymous_client), so it doesn't
# go looking for one.
NO_PROJECT = "<none>"


# The narrowest scope Google offers that can upload. It would also allow overwriting and
# deleting: what stops those is the folder's "Storage Object Creator" role, plus the
# create-only condition on every upload.
SCOPES = ["https://www.googleapis.com/auth/devstorage.read_write"]


class GCSStore:
    def __init__(self, bucket: str):
        # Sign in fully before any upload, so no copy is sent with a sign-in that then fails.
        try:
            credentials, _ = google.auth.default(scopes=SCOPES)
            credentials.refresh(Request())
        except (auth.DefaultCredentialsError, auth.RefreshError, auth.TransportError):
            raise SignInError(SIGN_IN) from None
        client = storage.Client(
            project=NO_PROJECT,
            credentials=credentials,
            client_options={"api_endpoint": ENDPOINT},
        )
        self.bucket = client.bucket(bucket)

    def add(self, name: str, data: bytes, content_type: str) -> Added:
        blob = self.bucket.blob(name)
        try:
            blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
        except api.PreconditionFailed:
            return "exists"  # create-only: the name is taken, and the stored file is unchanged
        except (auth.RefreshError, auth.DefaultCredentialsError, api.Unauthorized):
            raise SignInError(SIGN_IN) from None
        except api.Forbidden:
            raise StoreError(
                "Google Cloud Storage refused the upload: this account can't add files to the "
                "folder in config.toml's [store]. Check bucket and folder, or ask the project "
                "team for access."
            ) from None
        except api.NotFound:
            raise StoreError(
                "Google Cloud Storage has no bucket by the name in config.toml's [store]."
            ) from None
        except (
            api.GoogleAPICallError,
            api.RetryError,
            auth.TransportError,
        ) as e:
            # Retries ran out, or the network failed: the run reports how many were added.
            raise StoreError(
                f"Google Cloud Storage stopped the upload ({type(e).__name__})."
            ) from None
        return "added"
