# Use only the already accepted local API image as a dependency base.
# Build with --pull=false and verify this local image ID before building.
FROM sha256:77bc01222c9c018d44968198e26d0be4618825b35ba07bb8c993002bce4f51c5

USER 1000
COPY --chown=1000:1000 ops/runtime-ui/runtime_ui.py ops/runtime-ui/asset-pins.json ops/runtime-ui/navigation.html /runtime-ui/
COPY --chown=1000:1000 ops/runtime-ui/ui/ /runtime-ui/ui/
WORKDIR /runtime-ui
ENTRYPOINT ["python", "/runtime-ui/runtime_ui.py"]
