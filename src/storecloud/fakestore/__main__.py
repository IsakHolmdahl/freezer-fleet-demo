"""Run the fake store API: `python -m storecloud.fakestore` (port 8100, token `dev-token`)."""
import argparse

import uvicorn

from storecloud.fakestore.app import create_fake_app
from storecloud.fakestore.world import FakeStore, Timings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--token", default="dev-token")
    parser.add_argument("--technician-interval", type=float, default=60,
                        help="seconds between technician requests on freezers with software 2.x (0: none)")
    args = parser.parse_args()
    store = FakeStore(timings=Timings(technician_interval=args.technician_interval), token=args.token)
    print(f"store endpoint: http://127.0.0.1:{args.port}/api/v1/stores/{store.id}  token: {store.token}")
    uvicorn.run(create_fake_app(store), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
