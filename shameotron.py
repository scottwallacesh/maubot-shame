"""
[Maubot](https://mau.dev/maubot/maubot) plugin to shame room members into
upgrading their Matrix homeservers to the latest version.
"""
import json
from datetime import datetime
import socket
import ssl

from typing import Dict, List, Type

import requests

from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from mautrix.types import TextMessageEventContent, MessageType, Format, \
                          EventID, RoomID, UserID
from mautrix.util import markdown

from maubot import Plugin, MessageEvent
from maubot.handlers import command


class Config(BaseProxyConfig):
    """
    Config class
    """
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        """
        Class method to update the config
        """
        helper.copy('federation_tester')
        helper.copy('dead_servers')


class ShameOTron(Plugin):
    """
    Main class for the Shame-o-Tron
    """
    async def start(self) -> None:
        """
        Class method for plugin startup
        """
        self.on_external_config_update()


    @classmethod
    def get_config_class(cls) -> Type[Config]:
        """
        Class method for getting the config
        """
        return Config


    async def _edit(self, room_id: RoomID, event_id: EventID, text: str) -> None:
        """
        Class method to update an existing message event
        """
        content = TextMessageEventContent(
            msgtype=MessageType.NOTICE,
            body=text,
            format=Format.HTML,
            formatted_body=markdown.render(text)
        )
        content.set_edit(event_id)
        await self.client.send_message(room_id, content)


    async def _load_members(self, room_id: RoomID) -> Dict[str, List[UserID]]:
        """
        Class method to return the servers and room members
        """
        users = await self.client.get_joined_members(room_id)
        servers: Dict[str, List[UserID]] = {}
        for user in users:
            _, server = self.client.parse_user_id(user)
            servers.setdefault(server, []).append(user)
        return servers


    async def get_ssl_expiry(self, addr, host):
        """
        Class method to return the expiry date of a specific instance
        """
        ssl_date_fmt = r'%b %d %H:%M:%S %Y %Z'
        (hostname, port) = addr.split(':')

        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_OPTIONAL
        conn = context.wrap_socket(
            socket.socket(socket.AF_INET),
            server_hostname=host,
        )
        conn.settimeout(10.0)

        conn.connect((hostname, int(port)))
        ssl_info = conn.getpeercert()

        # parse the string from the certificate into a Python datetime object
        return datetime.strptime(ssl_info['notAfter'], ssl_date_fmt)


    async def query_homeserver(self, host):
        """
        Class method to query the Federation Tester to retrieve the running
        version for a server

        host: (str) Server to get version for

        Returns: (str) Version string of the server
        """
        version = None
        try:
            req = requests.get(
                self.config["federation_tester"].format(server=host),
                timeout=10000
            )
        except requests.exceptions.Timeout:
            version = '[TIMEOUT]'

        data = json.loads(req.text)

        if not data['FederationOK']:
            version = '[OFFLINE]'

        try:
            addr = list(data['ConnectionReports'].keys())[0]
            ssl_expiry = await self.get_ssl_expiry(addr, host)
        except (
                ssl.SSLCertVerificationError,
                ssl.SSLError,
                IndexError
        ) as error:
            self.log.warning('SSL error for: %s: %s', host, error)
            ssl_expiry = None

        try:
            if not version:
                version = data['Version']['version']
        except (TypeError, KeyError) as error:
            self.log.error(error)
            version = '[ERROR]'

        return {
            'version': version,
            'ssl_expiry': ssl_expiry
        }


    @command.new('shame', help='Show versions of all homeservers in the room')
    @command.argument("candidate", pass_raw=True, required=False)
    async def shame_handler(self, evt: MessageEvent, candidate: str = None) -> None:
        """
        Class method to handle the `!shame` command
        """
        event_id = await evt.reply('Loading member list...')
        if candidate:
            member_servers = [candidate]
        else:
            member_servers = await self._load_members(evt.room_id)

            # Filter out the "dead servers"
            dead_servers = self.config['dead_servers']
            if dead_servers:
                # Return a unique list
                member_servers = sorted(
                    list(
                        set(member_servers.keys() - set(dead_servers))
                    )
                )

        await self._edit(
            evt.room_id,
            event_id,
            'Member list loaded, fetching versions... please wait...'
        )

        versions = []
        for host in member_servers:
            data = await self.query_homeserver(host)

            warning = ''
            now = datetime.now()
            if data['ssl_expiry']:
                expiry_days = (data['ssl_expiry'] - now).days
                if expiry_days < 30:
                    warning = f'(cert expiry in {expiry_days} days!)'
            else:
                warning = '(SSL error)'

            versions.append(
                (host, f"{data['version']} {warning}")
            )

        await self._edit(
            evt.room_id,
            event_id,
            (
                '#### Homeserver versions\n'
                + '\n'.join(
                    f'* {host}: [{version}]({self.config["federation_tester"].format(server=host)})'
                    for host, version in versions
                )
            )
        )
