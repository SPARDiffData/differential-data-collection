"""The pilot store: a Google Cloud Storage bucket (STO-1, decision D3), behind `Store`.

The push signs in with the team's upload-only service account key, `store-key.json` next
to config.toml (store/push.py reads and checks it). In the bucket that account has "Storage
Object Creator" on the team's folder only: it can add files there, and can't read, change or
delete anything. Every upload is also create-only (`if_generation_match=0`), so the bucket
itself refuses to overwrite a file, even if our own checks were wrong (STO-3).

diffdata never prints, stores or sends the key's contents anywhere but to Google's library.
Push imports this module only when a real push runs. Its own tests use a stand-in for
Google's client and sign-in, with no bucket, key or network.
"""

from datetime import datetime

from google.api_core import exceptions as api
from google.auth import exceptions as auth
from google.auth.transport.requests import Request
from google.cloud import storage
from google.oauth2 import service_account

from diffdata.store.backend import Added, SignInError, StoreError

TURNED_DOWN = (
    "Google turned down the store key. It may have been replaced or revoked, or this "
    "computer's clock may be wrong. Check the clock, or ask the project team for a new key. "
    "Nothing was sent."
)
NOT_A_KEY = (
    "~/.diffdata/store-key.json isn't a Google service account key. Ask the project team for a "
    "new one. Nothing was sent."
)
UNREACHABLE = (
    "Couldn't reach Google Cloud Storage: the network dropped or Google didn't answer in time. "
    "Check your internet connection and run again."
)

# Google's own address, set here so no setting in the environment can send uploads elsewhere.
ENDPOINT = "https://storage.googleapis.com"
# Adding files to a bucket needs no Cloud project. The key names one; if it doesn't, this is
# the library's own placeholder (as in its create_anonymous_client).
NO_PROJECT = "<none>"
# The narrowest scope Google offers that can upload. It would also allow overwriting and
# deleting: what stops those is the folder's "Storage Object Creator" role, plus the
# create-only condition on every upload.
SCOPES = ["https://www.googleapis.com/auth/devstorage.read_write"]
# Reading the team's opt-out list needs only this. The key can read <folder>/_config/ only.
READ_SCOPES = ["https://www.googleapis.com/auth/devstorage.read_only"]


def signed_in(key: dict, scopes: list[str], nothing: str) -> storage.Client:
    """A client signed in with the team's key, fully, before any request. `nothing` ends the
    message for a network failure ("Nothing was sent.")."""
    try:
        credentials = service_account.Credentials.from_service_account_info(key, scopes=scopes)
    except (ValueError, KeyError, TypeError):
        # Its message could quote part of the key, so it's never shown.
        raise SignInError(NOT_A_KEY) from None
    try:
        credentials.refresh(Request())
    except auth.RefreshError:
        raise SignInError(TURNED_DOWN) from None
    except auth.TransportError:
        raise StoreError(f"{UNREACHABLE} {nothing}".rstrip()) from None
    project = key.get("project_id")
    return storage.Client(
        project=project if isinstance(project, str) and project else NO_PROJECT,
        credentials=credentials,
        client_options={"api_endpoint": ENDPOINT},
    )


class GCSStore:
    def __init__(self, bucket: str, key: dict):
        # Sign in fully before any upload, so no copy is sent with a key that then fails.
        self.bucket = signed_in(key, SCOPES, "Nothing was sent.").bucket(bucket)

    def add(self, name: str, data: bytes, content_type: str) -> Added:
        blob = self.bucket.blob(name)
        try:
            blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
        except api.PreconditionFailed:
            return "exists"  # create-only: the name is taken, and the stored file is unchanged
        except (auth.RefreshError, api.Unauthorized):
            raise SignInError(TURNED_DOWN) from None
        except api.Forbidden:
            raise StoreError(
                "Google Cloud Storage refused the upload: this team's key can't add files to the "
                "folder in config.toml's [store]. Check bucket and folder, or ask the project "
                "team for access."
            ) from None
        except api.NotFound:
            raise StoreError(
                "Google Cloud Storage has no bucket by the name in config.toml's [store]."
            ) from None
        except (api.RetryError, auth.TransportError):
            raise StoreError(UNREACHABLE) from None
        except api.GoogleAPICallError as e:
            # The code is a number, so it's safe to show. The message could quote anything.
            code = f" (code {e.code})" if isinstance(e.code, int) else ""
            raise StoreError(
                f"Google Cloud Storage stopped the upload with an error on its side{code}. Wait "
                "a few minutes and run again."
            ) from None
        return "added"


def fetch(bucket: str, key: dict, name: str) -> tuple[bytes, datetime] | None:
    """One file the team's key may read, and when the bucket says it was written; None if
    there's no such file. Used for the team's opt-out list (discover/team_optouts.py)."""
    client = signed_in(key, READ_SCOPES, "")
    try:
        blob = client.bucket(bucket).get_blob(name)
        if blob is None:
            return None
        # That same version, even if the list is written again meanwhile.
        data = blob.download_as_bytes(if_generation_match=blob.generation)
    except api.PreconditionFailed:
        raise StoreError("The list changed while it was read. Run again.") from None
    except api.NotFound:
        return None
    except (auth.RefreshError, api.Unauthorized):
        raise SignInError(TURNED_DOWN) from None
    except api.Forbidden:
        raise StoreError(
            "Google Cloud Storage refused the read: this team's key can't read its _config "
            "folder. Ask the project team."
        ) from None
    except (api.RetryError, auth.TransportError):
        raise StoreError(UNREACHABLE) from None
    except api.GoogleAPICallError as e:
        code = f" (code {e.code})" if isinstance(e.code, int) else ""
        raise StoreError(
            f"Google Cloud Storage stopped the read with an error on its side{code}. Wait a few "
            "minutes and run again."
        ) from None
    if not isinstance(blob.time_created, datetime):
        raise StoreError("Google Cloud Storage didn't say when the list was written.")
    return data, blob.time_created
