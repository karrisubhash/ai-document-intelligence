# Object storage and multi-host validation

**Neither object storage nor a multi-host deployment has ever run.** The pilot does not
need either one. It runs on one host with uploads in the `media_data` volume
([staging runbook](staging-runbook.md)). This page lists what has to be true, and how to
prove it, before either one is used.

What is known from the code:

| Area | State |
|---|---|
| S3 code path | settings switch the default storage to `storages.backends.s3.S3Storage` when `AWS_STORAGE_BUCKET_NAME` is set; private ACL; the code never hands out bucket URLs. Downloads stream through Django. The pipeline copies each object to a temporary file once per job and deletes it afterwards. Contract-tested against a stub storage only |
| S3 packages in the image | **Fixed for this release.** Build with `INSTALL_S3=true`; the pinned image imports `storages.backends.s3` (django-storages 1.14.4, boto3 1.43.108). Before this release no image installed them, so turning S3 on failed at the first upload |
| S3 settings | Bucket, region (`AWS_S3_REGION_NAME`) and endpoint (`AWS_S3_ENDPOINT_URL`) only. Credentials come from the standard AWS chain: environment variables, or an instance role. No setting exists for the addressing style, and MinIO may need path-style addressing |
| Backups with S3 | `backup --include-media` archives the local media directory, which is empty with S3. It warns that it archived nothing and still reports success. With S3, the originals depend on the bucket's own versioning and replication |
| Readiness with S3 | `/ready/` does not check storage. A broken bucket shows up only on System status, and when uploads fail |
| Running the test suite | `tests_object_storage` asserts the local backend. Run the suite without `AWS_STORAGE_BUCKET_NAME` in the environment |

## A. Object storage (MinIO first, then the real bucket)

Run this on staging, with a disposable database and only public test documents.

| # | Step | Pass |
|---|---|---|
| A1 | Build with `INSTALL_S3=true`. Then `$DC run --rm --no-deps backend python -c "import storages.backends.s3, boto3"` | exits 0 |
| A2 | Start MinIO on the project network (`docker run -d --name minio --network adi_default -e MINIO_ROOT_USER=… -e MINIO_ROOT_PASSWORD=… quay.io/minio/minio server /data`). Create a bucket with `mc mb` | the bucket exists; `mc anonymous get` reports `private` |
| A3 | In `.env`, set `AWS_STORAGE_BUCKET_NAME`, `AWS_S3_ENDPOINT_URL=http://minio:9000`, `AWS_S3_REGION_NAME=us-east-1`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. Then `$DC up -d` | containers healthy |
| A4 | `$DC exec backend python manage.py shell -c "from documents import storage; print(storage.describe(), storage.is_remote())"` | `S3Storage True`. A DNS error for `<bucket>.minio` means virtual-host addressing: record it, because fixing it needs a settings change (an `addressing_style` option) |
| A5 | System status as a system administrator | storage `ok`; object storage `true`; `mc ls m/<bucket>/health/` is empty, because the probe deletes itself |
| A6 | Upload a public PDF | an object appears at `documents/<id>/versions/1/<name>`; nothing new in `/app/backend/media` |
| A7 | Processing reaches *Ready* | the worker log shows `storage.materialized`; no temporary copy remains in the worker's `/tmp` |
| A8 | Download it | its sha256 matches the original and the version's checksum |
| A9 | Fetch the object URL without credentials from inside the network | `403` |
| A10 | Upload a second version with the same file name | a separate object under `versions/2/`; version 1 still downloads with `?version=1` |
| A11 | Delete the document | its objects are gone; the backend logs `storage.file_deleted`, not `storage.file_delete_failed` |
| A12 | `docker stop minio`, then upload | the upload fails with an error, and System status shows storage failed. `/ready/` stays 200, which confirms the known readiness gap |
| A13 | Enable bucket versioning, delete a document, list versions | the earlier object version is still listed. This is the only recovery path for originals with S3 |
| A14 | Repeat A3–A13 against the institution's real bucket. Leave `AWS_S3_ENDPOINT_URL` unset, use an IAM policy limited to `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` and `s3:ListBucket` on that bucket, and turn Block Public Access on | as above. If the bucket enforces "bucket owner" object ownership, check that uploads are not refused with `AccessControlListNotSupported`: the code sends `default_acl="private"` |

Record the results. Until A14 passes, S3 is **not** a supported production configuration.

## B. Two application hosts

This needs A to have passed first. The shipped files assume one host. Each item below is
configuration this repository does not provide yet:

| Need | Why | What is missing |
|---|---|---|
| Postgres and Redis reachable from host B | workers on B must see host A's jobs and sessions | the overlay never publishes them, and the deployment check rejects publishing them. A private network or managed services are required |
| A Redis password | today any container on the network can read sessions and inject tasks | `requirepass` plus `redis://:<password>@…` in every client URL |
| No second `db`, `redis` or `migrate` on host B | `depends_on` starts them wherever a worker starts; a second `migrate` races the first | start B's services with `--no-deps`, or write a separate host-B compose file |
| A load balancer in front of both edges | each nginx resolves `backend` only on its own host's Docker network | an institutional load balancer |
| Prometheus targets on host B | discovery uses Docker DNS on host A | static or file-based targets for `hostB:9100`, published privately |
| A model cache on each host | `hf_cache` is a per-host volume | run `warm_models` on each host before setting `HF_HUB_OFFLINE=1` |
| Certificates | `letsencrypt` is a per-host volume, and HTTP-01 challenges reach only one host | terminate TLS at the load balancer |

### Validation, once that configuration exists

| # | Step | Pass |
|---|---|---|
| B1 | On B: `$DC up -d --no-deps worker worker-index` | `docker ps` on B shows no `db`, `redis` or `migrate` |
| B2 | On B: `celery -A config inspect ping` | workers on both hosts reply |
| B3 | Stop A's worker, then upload through A | the document reaches *Ready*, processed by B (B's log shows `storage.materialized`) |
| B4 | On B: list `/app/backend/media` | empty; the object exists only in the bucket |
| B5 | On A: `showmigrations --plan \| grep -c '\[ \]'` | `0`, with exactly one `migrate` run recorded |
| B6 | Sign in through A's edge, then reuse the session cookie against B's edge | `200` |
| B7 | Exceed the login throttle across both edges | `429` at the configured total, not twice that |
| B8 | Prometheus targets | B's workers `up`; pipeline counters rise when B processes a document |
| B9 | `docker kill` B's worker during a job | the job is redelivered, or reclaimed by `recover_stalled_jobs` |

## Edge behind another load balancer

If an institutional load balancer sits in front of nginx:

- `DRF_NUM_PROXIES=1` (set in the overlay) makes throttles use the address nginx sees.
  Behind one more proxy, that is the load balancer's address, so **every user shares one
  throttle bucket**. Set `DRF_NUM_PROXIES=2`, and make sure the load balancer appends to
  `X-Forwarded-For`.
- If the load balancer talks plain HTTP to nginx on port 80, nginx redirects every
  request to HTTPS, which loops. Send HTTPS to 443, or change the port-80 server block.
  Changing it is a configuration edit to `infrastructure/nginx/nginx.conf` that has not
  been tested.
