import secrets
import re
import json
import asyncio
import websockets
import os

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from websockets import WebSocketServerProtocol as WSConnection
from websockets.exceptions import ConnectionClosedError
from dots_and_boxes import Grid, Player, DotsAndBoxes, Edge, DotsAndBoxesException

USERNAME_REGEX = r'^\w{4,9}$'
PASSWORD_REGEX = r'^\w{4,9}$'
HOST, PORT = '', os.environ.get('PORT', 8080)


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
# NOTE: Once user is created, cannot be deleted
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
        # [Only] called when connection closes -> ABANDONED Session (ie, no connection)
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

    def live_session(self, user: User) -> Optional[Session]:
        # Returns the active session of the user (if any)
        # At most one active session per user
        for session in self._sessions.values():
            if session.user == user:
                return session


class Game:
    TIMEOUT = 600
    CLEAN_TIMEOUT = 60  # Time after which game is deleted after game over

    def __init__(self, *users: User, grid=Grid(5, 5), session_manager: SessionManager):
        self._game_id = secrets.token_hex()
        self._grid = grid
        self._sm = session_manager
        self._users = users
        self._players = [Game.player(user) for user in users]
        # Can throw exception if invalid number of users
        self._game = DotsAndBoxes(self._players, grid=grid)
        self._active = True

        # NOTE: Messages regarding game creation must be sent by the creator
        # Remaining messages are sent!

    # NOTE: All the methods updating state should only be triggered via game manager
    def make_move(self, user: User, edge: Edge):
        try:
            self._game.make_move(Game.player(user), edge)
        except DotsAndBoxesException:
            raise
        else:
            # Send updated game to the (active) users if move is successful
            # Note: sending the game updates the users if the game is over
            for user in self._users:
                session = self._sm.live_session(user)
                if session and session.connection:
                    # Send the game to active connections
                    asyncio.create_task(Send.game(session.connection, self))

    def expire(self):
        # Expiry must only be triggered via Game Manager
        self._active = False
        for user in self._users:
            session = self._sm.live_session(user)
            if session and session.connection:
                # Intimate the users about game expiry
                asyncio.create_task(Send.game_expired(session.connection, self._game_id))

    # GETTERS
    @property
    def session_status(self):
        # Returns the session status of each of the users part of the game
        statuses = []
        for user in self._users:
            session = self._sm.live_session(user)
            if not session:
                statuses.append('SESSION_EXPIRED')
            elif session.connection is None:
                statuses.append('SESSION_ABANDONED')
            else:
                statuses.append('SESSION_ACTIVE')
        return statuses

    @property
    def expired(self):
        return not self._active

    @property
    def game_id(self):
        return self._game_id

    @property
    def data(self):
        # Returns the serialized version of the game
        return self._game.encode()

    @property
    def game_over(self):
        return self._game.game_over

    # Checks if user part of game
    def __contains__(self, user: User):
        return user in self._users

    @staticmethod
    def player(user: User):
        return Player(user_id=user.user_id, username=user.username)


class GameManager:
    def __init__(self, session_manager):
        self._games: Dict[str, Game] = {}
        self._sm = session_manager

    def __getitem__(self, game_id):
        return self._games.get(game_id)

    def create_game(self, *users: User, grid=Grid(5, 5)):
        # Users can be part of multiple games, that are active
        # Client must take of implementing necessary UI
        game = Game(*users, grid=grid, session_manager=self._sm)
        self._games[game.game_id] = game
        # Schedule expiry of game after timeout
        asyncio.get_event_loop().call_later(Game.TIMEOUT, self.expire_game, game.game_id)
        return game

    def make_move(self, game_id, user: User, edge: Edge):
        game = self._games[game_id]
        if game:
            try:
                game.make_move(user, edge)
                if game.expired:
                    # Schedule the clean up of the game
                    asyncio.get_event_loop().call_later(Game.CLEAN_TIMEOUT, self.expire_game, game.game_id)
            except DotsAndBoxesException:
                raise

    def expire_game(self, game_id):
        # Game expiry can be triggered multiple times
        game = self[game_id]
        if game:
            game.expire()
            del self._games[game_id]


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

    @staticmethod
    async def game(websocket: WSConnection, game: Game):
        await websocket.send(json.dumps({
            'type': 'GAME',
            'game_id': game.game_id,
            'game_data': game.data,
            'player_status': game.session_status,
        }))

    @staticmethod
    async def game_expired(websocket: WSConnection, game_id):
        await websocket.send(json.dumps({
            'type': 'GAME_EXPIRED',
            'session_id': game_id,
        }))

    # Unauthorized reasons
    MULTIPLE_REQUESTS = 'Multiple requests'
    INVALID_USER = 'Invalid user'
    GAME_EXCEPTION = 'Game exception'

    @staticmethod
    async def unauthorized(websocket: WSConnection, reason: str):
        await websocket.send(json.dumps({
            'type': 'UNAUTHORIZED',
            'error': reason,
        }))


class Orchestrator:
    NUM_PLAYERS = 2
    GRID = Grid(5, 5)

    def __init__(self):
        self.user_manager = UserManager()
        self.session_manager = SessionManager()
        self.game_manager = GameManager(self.session_manager)
        # Connections waiting for sufficient players to join to start the game
        # Map from connection to session_id.
        # Note: Session could have expired, need to check before starting a game.
        self._waiting_connections: Dict[WSConnection, str] = {}

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
        session = self.get_session(session_id, connection)
        # Logout the active session on the connection or the adopted abandoned session
        self.session_manager.logout_session(session_id)

    def get_session(self, session_id, connection):
        # Returns the active session identified by 'session_id' if session is Active (not expired)
        # and is the active session on the connection
        # However if INACTIVE connection, adopts the active session if session is in ABANDONED state (no connection)
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
                    # Adopt the ABANDONED session (with no active connection)
                    # And return the ADOPTED session
                    self.session_manager.reconnect(session_id, connection)
                    return session
                elif session.connection == connection:
                    # IMPOSSIBLE CASE
                    raise NotImplementedError('Should be an existing session')
                else:
                    # Attempting hijack of active connection
                    raise UnauthenticatedException(Send.CONNECTION_HIJACK)

    def join_game(self, session_id, connection):
        session = self.get_session(session_id, connection)
        if connection in self._waiting_connections:
            # In case already a request is pending
            raise UnauthorizedException(Send.MULTIPLE_REQUESTS)

        # Add the Existing session or the adopted session (session_id) into Waiting List
        self._waiting_connections[connection] = session_id
        for connection, session_id in list(self._waiting_connections.items()):
            session = self.session_manager[session_id]
            if not session:
                # Expired session, need not notify connection
                # Would have already been notified when session expired
                del self._waiting_connections[connection]

        if len(self._waiting_connections) == Orchestrator.NUM_PLAYERS:
            # Enough active players that we can start the game
            users = [self.session_manager[session_id].user for session_id in self._waiting_connections.values()]
            game = self.game_manager.create_game(*users, grid=Orchestrator.GRID)
            for connection in self._waiting_connections:
                # Intimate the users waiting to join the new game!
                asyncio.create_task(Send.game(connection, game))

    def get_game(self, session_id, game_id, connection):
        # Returns the corresponding game, if session and game is valid
        session = self.get_session(session_id, connection)
        game = self.game_manager[game_id]
        if not game:
            raise GameExpiredException(game_id)
        elif session.user not in game:
            raise UnauthorizedException(Send.INVALID_USER)

        # User is part of game
        return game

    def make_move(self, session_id, game_id, edge: Edge, connection):
        game = self.get_game(session_id, game_id, connection)
        session = self.session_manager[session_id]
        # Makes the move and sends the updated game to all the users
        game.make_move(session.user, edge)

    def exit_game(self, session_id, game_id, connection):
        game = self.get_game(session_id, game_id, connection)
        self.game_manager.expire_game(game.game_id)

    def unregister(self, connection):
        # ABANDON the session using the connection, if any
        self.session_manager.unregister(connection)
        # Remove from game waiting list
        if connection in self._waiting_connections:
            del self._waiting_connections[connection]


class UnauthenticatedException(Exception):
    # Thrown when user try to create a new session or switch to another session on an ACTIVE connection
    # or when login/signup fails or user tries to hijack a session (with ACTIVE connection)
    def __init__(self, message):
        print(f'[UnauthenticatedException] {message}')
        self.message = message
        super().__init__(message)


class UnauthorizedException(Exception):
    # Thrown when user tries to access a game (identified by game_id) that user
    # is not playing when on ACTIVE connection
    # Or when user tries to send multiple game requests (not waiting for a game to started)
    def __init__(self, message):
        print(f'[UnauthorizedException] {message}')
        self.message = message
        super().__init__(message)


class SessionExpiredException(Exception):
    # Thrown when requested session (identified by session_id) does not exist
    # when on INACTIVE session (no session to ADOPT)
    def __init__(self, session_id):
        print(f'[SessionExpiredException] session_id:{session_id}')
        self.session_id = session_id
        super().__init__(session_id)


class GameExpiredException(Exception):
    # Thrown when requested game (identified by game_id) does not exist
    # when on ACTIVE connection
    def __init__(self, game_id):
        print(f'[GameExpiredException] game_id:{game_id}')
        self.game_id = game_id
        super().__init__(game_id)


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
            try:
                # Session Management requests
                if data['type'] == 'SIGN_UP':
                    username, password = data['username'], data['password']
                    session = orchestrator.sign_up(username, password, websocket)
                    await Send.authenticated(websocket, session)

                elif data['type'] == 'LOGIN':
                    username, password = data['username'], data['password']
                    session = orchestrator.login(username, password, websocket)
                    await Send.authenticated(websocket, session)

                elif data['type'] == 'LOGOUT':
                    session_id = data['session_id']
                    orchestrator.logout(session_id, websocket)

                elif data['type'] == 'STATUS':
                    # When connection is ACTIVE, returns status of existing session
                    # When connection is INACTIVE, can take over active ABANDONED session
                    # (ie, session with no connection), if available
                    # Simplest use case of request with `session_id`
                    session_id = data['session_id']
                    session = orchestrator.get_session(session_id, websocket)
                    await Send.authenticated(websocket, session)

                elif data['type'] == 'JOIN_GAME':
                    session_id = data['session_id']
                    # Adds Active Connection(session/connection) to waiting list till enough user has joined
                    # Once enough Active Connections, game created using all those users and users are intimated
                    orchestrator.join_game(session_id, websocket)

                elif data['type'] == 'GET_GAME':
                    session_id = data['session_id']
                    game_id = data['game_id']
                    # Makes sure session, game is valid and user (from session) is part of game
                    # and returns the game (also adopts the session if ABANDONED)
                    game = orchestrator.get_game(session_id, game_id, websocket)
                    await Send.game(websocket, game)

                elif data['type'] == 'MAKE_MOVE':
                    session_id = data['session_id']
                    game_id = data['game_id']
                    edge = Edge.decode(data['edge_data'])
                    # Makes all the checks and actions of GET_GAME, and makes the move
                    # and sends the latest game to all the active game connections
                    orchestrator.make_move(session_id, game_id, edge, websocket)

                elif data['type'] == 'EXIT_GAME':
                    session_id = data['session_id']
                    game_id = data['game_id']
                    # Makes all the checks and actions of GET_GAME, and expires the move
                    # and sends the game expired to all active connections
                    orchestrator.exit_game(session_id, game_id, websocket)

            except UnauthenticatedException as e:
                await Send.unauthenticated(websocket, e.message)
            except SessionExpiredException as e:
                await Send.session_expired(websocket, e.session_id)
            except UnauthorizedException as e:
                await Send.unauthorized(websocket, e.message)
            except GameExpiredException as e:
                await Send.game_expired(websocket, e.game_id)
            except DotsAndBoxesException:  # MAKE_MOVE
                await Send.unauthorized(websocket, Send.GAME_EXCEPTION)

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
