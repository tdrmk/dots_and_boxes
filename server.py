import secrets
import re
import json
import asyncio
import websockets

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from websockets import WebSocketServerProtocol as WSConnection
from websockets.exceptions import ConnectionClosedError

USERNAME_REGEX = r'^\w{4,9}$'
PASSWORD_REGEX = r'^\w{4,9}$'


# Maintains user information
@dataclass(frozen=True)
class User:
    user_id: str
    username: str
    password: str

    @classmethod
    def new_user(cls, username, password):
        user_id = secrets.token_hex()
        return cls(user_id, username, password)

    def matches_user(self, username, password):
        return self.username == username and self.password == password

    @staticmethod
    def validate(username, password):
        # checks if username and password are valid
        return re.match(USERNAME_REGEX, username) and re.match(PASSWORD_REGEX, password)


# Persistently manages users
class UserManager:
    FILENAME = 'users.json'

    def __init__(self):
        # user_id -> User
        self._users: Dict[str, User] = {}
        self._load()  # load users from file

    def __getitem__(self, user_id) -> Optional[User]:
        return self._users.get(user_id)

    def get_user(self, username, password) -> Optional[User]:
        for user in self._users.values():
            if user.matches_user(username, password):
                return user

    def create_user(self, username, password) -> Optional[User]:
        for user in self._users.values():
            if user.username == username:
                # username already taken
                return

        if User.validate(username, password):
            # Create new user
            user = User.new_user(username, password)
            print(f'User created {user}')
            self._users[user.user_id] = user
            self._dump()  # save current users to file
            return user

    # Save and Load users to/from a file
    def _load(self):
        try:
            with open(UserManager.FILENAME, 'r') as f:
                # update with users from file
                for user_details in json.load(f):
                    self._users[user_details['user_id']] = User(**user_details)
        except FileNotFoundError:
            pass
        except json.JSONDecodeError:
            print(f'Unable to parse {UserManager.FILENAME}.')

    def _dump(self):
        with open(UserManager.FILENAME, 'w') as f:
            json.dump([asdict(user) for user in self._users.values()], f, indent=4)


class Session:
    TIMEOUT = 600  # 10 minutes

    def __init__(self, user: User, connection: WSConnection):
        self._session_id = secrets.token_hex()
        self._user = user
        # Current connection via which session was created
        # Will be later updated to None if connection closes
        # Re-updated with new connection when attempt to establish existing session
        # However note update is not possible when existing connection still exists
        self._connection = connection
        self._active = True  # keeps track of session expiry

        print(f'Session created {self._session_id} {user}')

    # NOTE: All the methods updating state must only be triggered via the session manager
    def expire(self):
        # Note: Expiry should only be triggered via the session manager
        self._active = False
        if self._connection:
            # In case of expiry let the particular connection know if it exists
            asyncio.create_task(Send.session_expired(self._connection, self._session_id))
            # Disassociate from the connection once message is triggered
            self._connection = None

        print(f'Session expired {self._session_id} {self.user}')

    def disconnect(self):
        # [Only] called when connection closes
        self._connection = None
        print(f'Session disconnected {self._session_id} {self.user}')

    def reconnect(self, connection):
        # Connection can only be updated when no existing connection
        # ie Active connection cannot be hijacked!
        if self._connection is None:
            self._connection = connection
            print(f'Session reconnected {self._session_id} {self.user}')
        else:
            print(f'Session hijack attempted {self._session_id} {self.user}')

    @property
    def session_id(self):
        return self._session_id

    @property
    def user(self):
        return self._user

    @property
    def connection(self) -> Optional[WSConnection]:
        return self._connection

    @property
    def expired(self):
        return not self._active


class SessionManager:
    def __init__(self):
        self._sessions: Dict[str, Session] = {}

    def __getitem__(self, session_id):
        return self._sessions.get(session_id)

    def create_session(self, user: User, connection: WSConnection):
        for session_id, session in list(self._sessions.items()):
            if session.user == user:
                # Logout existing session(s) of the user
                self.logout_session(session_id)

        session = Session(user, connection)
        self._sessions[session.session_id] = session
        # Schedule expiry of session after timeout
        asyncio.get_event_loop().call_later(Session.TIMEOUT, self.logout_session, session.session_id)
        return session

    def logout_session(self, session_id):
        # Logout can be triggered multiple times for a session
        session = self[session_id]
        if session:
            session.expire()
            del self._sessions[session_id]

    def active_session(self, connection: WSConnection) -> Optional[Session]:
        # Returns the session running over the connection (if any)
        # At most one active session per connection, and at most one connection per session
        for session in self._sessions.values():
            if session.connection == connection:
                return session

    def unregister(self, connection: WSConnection):
        # Called when connection is closed
        for session in self._sessions.values():
            if session.connection == connection:
                session.disconnect()

    def reconnect(self, session_id, connection: WSConnection):
        session = self[session_id]
        if session and not session.connection:
            session.reconnect(connection)
            return True
        # Returns False when no session or session hijack attempted
        return False


# Sending messages
class Send:
    @staticmethod
    async def authenticated(websocket: WSConnection, session: Session):
        await websocket.send(json.dumps({
            'type': 'AUTHENTICATED',
            'session_id': session.session_id,
            'user_id': session.user.user_id,
        }))

    # Unauthenticated reasons
    ACTIVE_CONNECTION = 'Active connection'
    SIGNUP_FAILED = 'Sign up failed'
    LOGIN_FAILED = 'Login failed'
    CONNECTION_HIJACK = 'Connection hijack'

    @staticmethod
    async def unauthenticated(websocket: WSConnection, reason: str):
        await websocket.send(json.dumps({
            'type': 'UNAUTHENTICATED',
            'error': reason,
        }))

    @staticmethod
    async def session_expired(websocket: WSConnection, session_id: str):
        await websocket.send(json.dumps({
            'type': 'SESSION_EXPIRED',
            'session_id': session_id,
        }))


HOST, PORT = 'localhost', 8080


class Orchestrator:
    def __init__(self):
        self.user_manager = UserManager()
        self.session_manager = SessionManager()

    def sign_up(self, username, password, connection):
        existing_session = self.session_manager.active_session(connection)
        if existing_session:
            raise UnauthenticatedException(Send.ACTIVE_CONNECTION)

        user = self.user_manager.create_user(username, password)
        if not user:
            raise UnauthenticatedException(Send.SIGNUP_FAILED)

        session = self.session_manager.create_session(user, connection)
        return session

    def login(self, username, password, connection):
        existing_session = self.session_manager.active_session(connection)
        if existing_session:
            raise UnauthenticatedException(Send.ACTIVE_CONNECTION)

        user = self.user_manager.get_user(username, password)
        if not user:
            raise UnauthenticatedException(Send.LOGIN_FAILED)

        session = self.session_manager.create_session(user, connection)
        return session

    def logout(self, session_id, connection):
        existing_session = self.session_manager.active_session(connection)
        if existing_session:
            if existing_session.session_id != session_id:
                raise UnauthenticatedException(Send.CONNECTION_HIJACK)
            else:
                # Active session, so no need to send any response message
                # logout sends message and disassociates itself from connection
                self.session_manager.logout_session(session_id)
        else:
            session = self.session_manager[session_id]
            if not session:
                # Need to send message indicating session has expired
                raise SessionExpiredException(session_id)
            else:
                # If its a running session
                if session.connection is None:
                    # Adopt the session and expire it
                    self.session_manager.logout_session(session_id)
                    raise SessionExpiredException(session_id)
                elif session.connection == connection:
                    # IMPOSSIBLE CASE
                    raise NotImplementedError('Should be an existing session')
                else:
                    # Attempting hijack of active connection
                    raise UnauthenticatedException(Send.CONNECTION_HIJACK)

    def status(self, session_id, connection):
        # Returns session if session is live
        existing_session = self.session_manager.active_session(connection)
        if existing_session:
            if existing_session.session_id != session_id:
                raise UnauthenticatedException(Send.CONNECTION_HIJACK)
            # Its requesting status of active session
            return existing_session
        else:
            session = self.session_manager[session_id]
            if not session:
                # Session has expired
                raise SessionExpiredException(session_id)
            else:
                if session.connection is None:
                    # Adopt the session
                    self.session_manager.reconnect(session_id, connection)
                    return session
                elif session.connection == connection:
                    # IMPOSSIBLE CASE
                    raise NotImplementedError('Should be an existing session')
                else:
                    # Attempting hijack of active connection
                    raise UnauthenticatedException(Send.CONNECTION_HIJACK)

    def unregister(self, connection):
        self.session_manager.unregister(connection)


class UnauthenticatedException(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


class SessionExpiredException(Exception):
    def __init__(self, session_id):
        self.session_id = session_id
        super().__init__(session_id)


async def handler(websocket: WSConnection, _):
    # Note: connection can be in one of two states: ACTIVE or INACTIVE
    # connection is ACTIVE when it has an associated active session
    # otherwise session is INACTIVE
    # Note: Certain messages can be received only in one of the states,
    # while some can be conditionally received
    try:
        # Consumer modal
        async for message in websocket:
            data = json.loads(message)
            # Session Management requests
            if data['type'] == 'SIGN_UP':
                username, password = data['username'], data['password']
                try:
                    session = orchestrator.sign_up(username, password, websocket)
                    await Send.authenticated(websocket, session)
                except UnauthenticatedException as e:
                    await Send.unauthenticated(websocket, e.message)

            elif data['type'] == 'LOGIN':
                username, password = data['username'], data['password']
                try:
                    session = orchestrator.login(username, password, websocket)
                    await Send.authenticated(websocket, session)
                except UnauthenticatedException as e:
                    await Send.unauthenticated(websocket, e.message)

            elif data['type'] == 'LOGOUT':
                session_id = data['session_id']
                try:
                    orchestrator.logout(session_id, websocket)
                except UnauthenticatedException as e:
                    await Send.unauthenticated(websocket, e.message)
                except SessionExpiredException:
                    await Send.session_expired(websocket, session_id)

            elif data['type'] == 'STATUS':
                session_id = data['session_id']
                try:
                    session = orchestrator.status(session_id, websocket)
                    await Send.authenticated(websocket, session)
                except UnauthenticatedException as e:
                    await Send.unauthenticated(websocket, e.message)
                except SessionExpiredException:
                    await Send.session_expired(websocket, session_id)
    except ConnectionClosedError:
        # Gracefully handle connection closure
        pass
    finally:
        # Unregister the connection
        orchestrator.unregister(websocket)


orchestrator = Orchestrator()
start_server = websockets.serve(handler, HOST, PORT)
asyncio.get_event_loop().run_until_complete(start_server)
asyncio.get_event_loop().run_forever()
