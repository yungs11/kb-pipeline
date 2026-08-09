#!/usr/bin/env bash
set -euo pipefail
BUCKET=document-parser
JOB_PREFIX=kbp-jobs
STAGING_PREFIX=parse-staging
ANON_POLICY=$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Principal": {"AWS": ["*"]}, "Action": ["s3:GetObject"],
     "Resource": ["arn:aws:s3:::${BUCKET}/*"]},
    {"Effect": "Deny", "Principal": {"AWS": ["*"]}, "Action": ["s3:GetObject"],
     "Resource": [
       "arn:aws:s3:::${BUCKET}/${JOB_PREFIX}/*",
       "arn:aws:s3:::${BUCKET}/${STAGING_PREFIX}/*",
       "arn:aws:s3:::${BUCKET}/*/original/*"
     ]}
  ]
}
POLICY
)
# simulate "podman exec -i" by just cat'ing what would be piped
cat <<< "$ANON_POLICY"
