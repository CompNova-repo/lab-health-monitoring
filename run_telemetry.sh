#!/bin/bash
# Point to the metric registry YAML so p1_fixed.py discovers all registered custom metrics.
export P1_METRIC_REGISTRY_FILE="$(dirname "$0")/metric_registry.yaml"

python "$(dirname "$0")/p1_fixed.py" run --mode standard
