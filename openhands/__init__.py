# Namespace package for openhands.
# This file uses PEP 420 implicit namespace package pattern with pkgutil extension.
# It allows multiple packages (openhands-sdk, openhands-workspace, openhands-automation)
# to share the openhands.* namespace.
__path__ = __import__("pkgutil").extend_path(__path__, __name__)
