from xconn_core import App
from xconn_core.app import ExecutionMode

from deskconn.api.auth import component as auth_component
from deskconn.api.user import component as user_component
from deskconn.api.device import component as device_component
from deskconn.api.desktop import component as desktop_component
from deskconn.api.principal import component as principal_component


app = App()
app.set_execution_mode(ExecutionMode.ASYNC)


app.include_component(user_component)
app.include_component(auth_component)
app.include_component(device_component)
app.include_component(desktop_component)
app.include_component(principal_component)
app.set_schema_procedure("io.xconn.deskconn.account.schema.get")


def run():
    import os

    from dotenv import load_dotenv
    from xconn_core import connect
    from xconn_core._client.types import ClientConfig

    load_dotenv()
    connect(
        "main:app",
        ClientConfig(
            url=os.environ["ROUTER_URL"],
            realm="io.xconn.deskconn",
            authid=os.environ["DESKCONN_ACCOUNT_AUTHID"],
            authmethod="cryptosign",
            private_key=os.environ["DESKCONN_ACCOUNT_PRIVATE_KEY"],
        ),
    )


if __name__ == "__main__":
    run()
