"""The pilot store: a Google Cloud Storage bucket (STO-1, decision D3), behind `Store`.

The producer signs in once with their own Google account:
    gcloud auth application-default login
and the team's folder in the bucket gives them "Storage Object Creator" only: they can add
files there and nothing else. Every upload is create-only (`if_generation_match=0`), so the
bucket itself refuses to overwrite a file, even if our own checks were wrong (STO-3).

diffdata never reads, stores or prints the sign-in token: Google's library finds it.
This module is imported only when a real push runs, so tests never load it.
"""

from google.api_core import exceptions as api
from google.auth import exceptions as auth
from google.cloud import storage

from diffdata.store.backend import Added, SignInError, StoreError

SIGN_IN = (
    "Couldn't sign in to Google Cloud Storage. Run `gcloud auth application-default login` "
    "once, sign in with the Google account the project team gave access to, then run again. "
    "Nothing was sent."
)


class GCSStore:
    def __init__(self, bucket: str):
        try:
            client = storage.Client()
        except auth.DefaultCredentialsError:
            raise SignInError(SIGN_IN) from None
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
