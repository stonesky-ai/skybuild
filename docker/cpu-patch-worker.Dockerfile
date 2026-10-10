# Build context must contain runner-git-rootfs/, generated from the exact
# SHA-pinned Debian package manifest with the reviewed offline staging helper.
FROM python@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5 AS python_base

# Start from the pinned Python base without carrying its inherited config.
FROM scratch
COPY --from=python_base / /
COPY runner-git-rootfs/ /

ENV PATH=/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1
USER 65534:65534
ENTRYPOINT ["python3"]
