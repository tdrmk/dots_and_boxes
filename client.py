from dots_and_boxes import *
from websockets import WebSocketClientProtocol as WSConnection, InvalidURI, InvalidStatusCode
from urllib.parse import urljoin
from aiohttp.client_exceptions import ClientError
import pygame
import asyncio
import websockets
import json
import re
import ssl
import socket
import argparse
import aiohttp

parser = argparse.ArgumentParser(description='Game client')
parser.add_argument('--username', type=str, default='username', help='Username')
parser.add_argument('--password', type=str, default='password', help='Password')
parser.add_argument('--uri', type=str, default='ws://localhost:8080', help='Server WebSocket URI')
parser.add_argument("--signup", action="store_true", help="Sign up else login")
parser.add_argument("--debug", action="store_true", help="Enable debug mode")
parser.add_argument("--insecure", action="store_true", help="Enable debug mode")
args = parser.parse_args()

URI = args.uri
USERNAME = args.username
PASSWORD = args.password
SIGNUP = bool(args.signup)  # else LOGIN
DEBUG = bool(args.debug)
INSECURE = bool(args.insecure)

BACKGROUND = pygame.Color('#FFFFFF')
FOREGROUND = pygame.Color('#000000')
EDGE_DEFAULT = pygame.Color('#CCCCCC')
EDGE_HOVERED = pygame.Color('#838383')

ACTIVE = pygame.Color('#00FF00')
INACTIVE = pygame.Color('#CCCCCC')


class EdgeUI:
    # Utility to draw and check collisions with an Edge
    def __init__(self, edge: Edge, offset=(50, 50), length=100, thickness=10):
        if edge.vertical:
            left = offset[1] + length * edge.start.y - thickness // 2
            top = offset[0] + length * edge.start.x + thickness // 2
            width = thickness
            height = length - thickness
        else:
            left = offset[1] + length * edge.start.y + thickness // 2
            top = offset[0] + length * edge.start.x - thickness // 2
            width = length - thickness
            height = thickness
        self.edge = edge
        self._rect = pygame.Rect(left, top, width, height)

    def draw(self, win: pygame.Surface, color):
        pygame.draw.rect(win, color, self._rect)

    def draw_highlight(self, win: pygame.Surface, color):
        pygame.draw.rect(win, color, self._rect, 3)

    def collide(self, x, y):
        return self._rect.collidepoint(x, y)


class BoxDrawer:
    # Utility to draw a boxes
    def __init__(self, offset=(50, 50), length=100, thickness=10):
        self.offset = offset
        self.length = length
        self.thickness = thickness

    def draw(self, win, box, color):
        top = self.offset[0] + self.length * box.start.x + self.thickness // 2
        left = self.offset[1] + self.length * box.start.y + self.thickness // 2
        width = self.length - self.thickness
        height = self.length - self.thickness

        pygame.draw.rect(win, color, pygame.Rect(left, top, width, height))


class ColorUtil:
    def __init__(self, total):
        self.total = total

    def secondary(self, index):
        color = pygame.Color('#FFFFFF')
        color.hsva = 360 * index / self.total, 100, 100, 100
        return color

    def primary(self, index):
        color = pygame.Color('#FFFFFF')
        color.hsva = 360 * index / self.total, 50, 50, 100
        return color


class GameUI:
    OFFSET = (50, 50)
    LENGTH = 100
    THICKNESS = 10

    def __init__(self, game: DotsAndBoxes, websocket: WSConnection, session_id: str, game_id: str, user_id: str):
        # Other user details are global information (ie, username and password)
        self.user_id = user_id
        self.player = Player(user_id, USERNAME)

        # Connection, session and game details
        # Transient can change
        self.websocket = websocket
        self.session_id = session_id
        self.game_id = game_id

        # Will be updated locally while making a move, rest of the times obtained from server
        self.game = game
        self.connection_status = {player: 'SESSION_ACTIVE' for player in game.players}

        # pygame and UI
        pygame.init()
        pygame.font.init()
        self.width, self.height = 100 * game.grid.columns + 100, 100 * game.grid.rows + 200

        self.large_font = pygame.font.Font(None, 50)
        self.medium_font = pygame.font.Font(None, 40)
        self.small_font = pygame.font.Font(None, 25)

        self.win = pygame.display.set_mode((self.width, self.height))
        pygame.display.set_caption(f'Dots and Boxes [{USERNAME}]')
        self.edges = [EdgeUI(edge) for edge in Edge.all_edges(game.grid)]
        self.box_drawer = BoxDrawer()
        self.color_util = ColorUtil(self.game.num_players)

        self.run = False

        # Stores true when new join_game request is sent, and waiting for response
        # Set when initiating a new game request, and used to rate limit the request to 1
        self.pending_new_request = False

    # Context manager
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pygame.quit()
        if self.websocket and not self.websocket.closed:
            await self.websocket.close()

    def draw(self):
        self.win.fill(BACKGROUND)
        if self.game:
            self._draw_game()
            self._draw_status()
            if self.game.game_over:
                self._draw_game_over()
        elif self.pending_new_request:
            self._draw_waiting_new_game()
        else:
            self._draw_game_expired()

    def _draw_game_over(self):
        modal_rect = pygame.Rect(self.width // 4, self.height // 4, self.width // 2, self.height // 2)
        pygame.draw.rect(self.win, BACKGROUND, modal_rect)
        pygame.draw.rect(self.win, FOREGROUND, modal_rect, 2)

        text = self.large_font.render("GAME OVER", True, FOREGROUND)
        text_rect = text.get_rect()
        text_rect.centerx, text_rect.centery = self.width // 2, self.height * 3 // 8
        self.win.blit(text, text_rect)

        winners = list(self.game.winners)
        if len(winners) > 1:
            text = self.medium_font.render("it's a tie", True, FOREGROUND)
        else:
            winner = winners[0]
            text = self.medium_font.render(
                f"{winner.username} won", True, self.color_util.primary(self.game.index(winner)))
        text_rect = text.get_rect()
        text_rect.centerx, text_rect.centery = self.width // 2, self.height // 2
        self.win.blit(text, text_rect)

        text = self.small_font.render('Press R for rematch', True, FOREGROUND)
        text_rect = text.get_rect()
        text_rect.centerx, text_rect.centery = self.width // 2, self.height * 2 // 3
        self.win.blit(text, text_rect)

        text = self.small_font.render('Press N for new game', True, FOREGROUND)
        text_rect = text.get_rect()
        text_rect.centerx, text_rect.top = self.width // 2, self.height * 2 // 3 + 20
        self.win.blit(text, text_rect)

    def _draw_game_expired(self):
        text = self.large_font.render("GAME EXPIRED!", True, FOREGROUND)
        text_rect = text.get_rect()
        text_rect.centerx, text_rect.centery = self.width // 2, self.height * 3 // 7
        self.win.blit(text, text_rect)

        text = self.small_font.render('Press N for new game', True, FOREGROUND)
        text_rect = text.get_rect()
        text_rect.centerx, text_rect.top = self.width // 2, self.height * 2 // 3 + 20
        self.win.blit(text, text_rect)

    def _draw_waiting_new_game(self):
        text = self.large_font.render("Waiting for new game!", True, FOREGROUND)
        text_rect = text.get_rect()
        text_rect.centerx, text_rect.centery = self.width // 2, self.height // 2
        self.win.blit(text, text_rect)

    def _draw_game(self):
        # Highlight Hovered edge
        x, y = pygame.mouse.get_pos()
        chosen_edges = self.game.chosen_edges_to_player
        for edge in self.edges:
            color = EDGE_DEFAULT
            if edge.edge in chosen_edges:
                player = chosen_edges[edge.edge]
                color = self.color_util.primary(self.game.index(player))
            elif edge.collide(x, y):
                color = EDGE_HOVERED

            edge.draw(self.win, color)
            if self.game.last_move == edge.edge:
                edge.draw_highlight(self.win, FOREGROUND)

        won_boxes = self.game.won_boxes_to_player
        for box, player in won_boxes.items():
            color = self.color_util.secondary(self.game.index(player))
            self.box_drawer.draw(self.win, box, color)

    def _draw_status(self):
        surfaces, rects = [], []

        # Note: To account for any number of players, logic to render scores is generalized.
        for player in self.game.players:
            index, score, username = self.game.index(player), self.game.score(player), player.username

            # Render score
            text = self.small_font.render(f"{username}: {score:02}", True, self.color_util.primary(index))
            text_rect = text.get_rect()

            # Construct a new surface which will contain score, connection status, and turn indicator
            surf = pygame.Surface((text_rect.w + 50 + text_rect.h, text_rect.h + 40))
            rect = surf.get_rect()
            surf.fill(BACKGROUND)
            # Add a border to indicate turn
            if player == self.game.current_player:
                pygame.draw.rect(surf, self.color_util.secondary(index), rect, 2)
            # Add a status indicator
            status_color = ACTIVE if self.connection_status[player] == 'SESSION_ACTIVE' else INACTIVE
            pygame.draw.circle(surf, status_color, (20 + text_rect.h // 2, 20 + text_rect.h // 2), text_rect.h // 2)
            # Add the score
            surf.blit(text, (30 + text_rect.h, 20))

            surfaces.append(surf)
            rects.append(rect)

        # Elements are rendered like flex's space-between
        spacing = max((self.width - 100 - sum(rect.w for rect in rects)) / (self.game.num_players - 1), 0)
        current_offset = 50  #
        for index, (surf, rect) in enumerate(zip(surfaces, rects)):
            rect.centery = self.height - 50
            rect.left = current_offset
            self.win.blit(surf, rect)
            current_offset = rect.right + spacing

        if self.game.game_over:
            if len(self.game.winners) > 1:
                text = self.small_font.render(f"it's a draw", True, FOREGROUND)
            else:
                winner = list(self.game.winners)[0]
                index = self.game.index(winner)
                text = self.small_font.render(f"{winner.username} won", True, self.color_util.primary(index))
        elif self.player == self.game.current_player:
            text = self.small_font.render(f"you're turn to move", True, FOREGROUND)
        else:
            text = self.small_font.render(f"wait for you're turn", True, FOREGROUND)
        rect = text.get_rect()
        rect.center = (self.width // 2, self.height - 100)
        self.win.blit(text, rect)

    async def keep_alive_ping(self):
        # Construct the HTTP GET health URL of the server
        url = urljoin(re.sub('^ws', 'http', URI), '/health')
        while self.run:
            # To prevent heroku from idling, periodically make a request to the client when game is active
            await asyncio.sleep(600)    # 10 minutes (heroku idling time 30 minutes)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, ssl=False) as response:
                        print(f'Server health status: {response.status} {response.reason}')

            except ClientError as e:
                print('Server health status: Error: ', e.__class__.__name__)
            except OSError:
                print('Server health status: server is not running')

    async def consume_messages(self):
        attempt_reconnect = False
        while self.run:
            try:
                async for message in self.websocket:
                    # Handle messages from active connection
                    response = json.loads(message, cls=DotsAndBoxesJSONDecoder)

                    if response['type'] == 'AUTHENTICATED':
                        print("Authenticated!")
                        # Update with the latest session_id, is update of user_id required?
                        self.session_id = response['session_id']
                        self.user_id = response['user_id']
                        # Join back game
                        asyncio.create_task(self.websocket.send(json.dumps({
                            'type': 'GET_GAME',
                            'session_id': self.session_id,
                            'game_id': self.game_id,
                        })))

                    elif response['type'] == 'UNAUTHENTICATED':
                        # TODO: What to do?
                        print(f"Unauthenticated, reason: {response['error']}")
                        self.run = False

                    elif response['type'] == 'SESSION_EXPIRED':
                        session_id = response['session_id']
                        if session_id == self.session_id:
                            print("Session expired! attempting to login back")
                            # Try logging it back
                            asyncio.create_task(self.websocket.send(json.dumps({
                                'type': 'LOGIN',
                                'username': USERNAME,
                                'password': PASSWORD,
                            })))

                    elif response['type'] == 'GAME':
                        # TODO: Make sure to check if its the right game
                        print("Got game from server...")
                        self.game_id = response['game_id']
                        self.game: DotsAndBoxes = response['game_data']
                        statuses = response['player_status']
                        for player in self.game.players:
                            self.connection_status[player] = statuses[self.game.index(player)]

                        # Reset this once a game is received
                        self.pending_new_request = False

                    elif response['type'] == 'GAME_EXPIRED':
                        print(f"Game expired {response['game_id']}!")
                        if self.game_id == response['game_id']:
                            # Make sure to check the game
                            self.game = None

                    elif response['type'] == 'PLAYER_STATUS':
                        print("Updated player status...")
                        if self.game_id == response['game_id']:
                            statuses = response['player_status']
                            for player in self.game.players:
                                self.connection_status[player] = statuses[self.game.index(player)]

                    elif response['type'] == 'UNAUTHORIZED':
                        # Quit game
                        print(f"Unauthorized, reason: {response['error']}")
                        self.run = False
            except websockets.exceptions.ConnectionClosedError:
                print('Connection lost')
                attempt_reconnect = True

            except asyncio.CancelledError:
                attempt_reconnect = False
                raise

            finally:
                if self.game and self.run and attempt_reconnect:
                    # If active game and user disconnects
                    self.connection_status = {player: 'SESSION_ABANDONED' for player in self.game.players}
                    # Try reconnecting..
                    print('Trying to reconnect...')

                    # If reconnection fails, exception is throw and loop breaks
                    await asyncio.sleep(10)
                    self.websocket = await establish_connection()
                    await self.websocket.send(json.dumps({
                        'type': 'LOGIN',
                        'username': USERNAME,
                        'password': PASSWORD,
                    }))
                    print('Reconnected successfully...')

                else:
                    return

    async def game_loop(self, interval=0.05):
        self.run = True

        # Updates based on server messages
        asyncio.create_task(self.consume_messages())
        # Periodically ping the server, just to keep it alive (prevent Heroku idling)
        asyncio.create_task(self.keep_alive_ping())

        while self.run:

            # Render the UI
            self.draw()
            pygame.display.update()

            for event in pygame.event.get():
                if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_q):
                    if self.game and not self.game.game_over:
                        # If game is still running
                        # Send exit message to server
                        print('Exiting the game...')
                        await self.websocket.send(json.dumps({
                            'type': 'EXIT_GAME',
                            'session_id': self.session_id,
                            'game_id': self.game_id,
                        }))
                    self.run = False

                if self.game and not self.game.game_over:
                    # Events to handle when game is running
                    if self.game.current_player == self.player:
                        if event.type == pygame.MOUSEBUTTONDOWN:
                            for edge in self.edges:
                                if edge.collide(event.pos[0], event.pos[1]):
                                    if edge.edge in self.game.pending_edges:
                                        print('Made a move...')
                                        # Make the move locally
                                        self.game.make_move(self.game.current_player, edge.edge)
                                        # Send the move to server
                                        asyncio.create_task(self.websocket.send(json.dumps({
                                            'type': 'MAKE_MOVE',
                                            'session_id': self.session_id,
                                            'game_id': self.game_id,
                                            'edge_data': edge.edge,
                                        }, cls=DotsAndBoxesJSONEncoder)))

                    # In case user is not sure if game is running or does not know status of the game
                    if event.type == pygame.KEYDOWN and event.key == pygame.K_l:
                        print('synchronizing game from server...')
                        asyncio.create_task(self.websocket.send(json.dumps({
                            'type': 'GET_GAME',
                            'session_id': self.session_id,
                            'game_id': self.game_id,
                        })))

                    if event.type == pygame.KEYDOWN and event.key == pygame.K_e:
                        # Exit Game, so that user can join new one
                        print('exiting current game...')
                        asyncio.create_task(self.websocket.send(json.dumps({
                            'type': 'EXIT_GAME',
                            'session_id': self.session_id,
                            'game_id': self.game_id,
                        })))

                    # For testing purposes
                    if DEBUG and event.type == pygame.KEYDOWN and event.key == pygame.K_c:
                        if self.websocket:
                            asyncio.create_task(self.websocket.close())

                elif self.game and self.game.game_over and not self.pending_new_request:
                    # Handle events when game over
                    if event.type == pygame.KEYDOWN and event.key == pygame.K_r:
                        print('Resetting the game...')
                        # Reset the game (new game with same player)
                        asyncio.create_task(self.websocket.send(json.dumps({
                            'type': 'RESET_GAME',
                            'session_id': self.session_id,
                            'game_id': self.game_id,
                        })))
                        # Also used while resetting game to prevent duplicate requests
                        self.pending_new_request = True

                    if event.type == pygame.KEYDOWN and event.key == pygame.K_n:
                        # Exit existing game
                        print('Exiting current game...')
                        asyncio.create_task(self.websocket.send(json.dumps({
                            'type': 'EXIT_GAME',
                            'session_id': self.session_id,
                            'game_id': self.game_id,
                        })))
                        # Join new game
                        print('Sending join new game request...')
                        asyncio.create_task(self.websocket.send(json.dumps({
                            'type': 'JOIN_GAME',
                            'session_id': self.session_id,
                        })))

                        self.pending_new_request = True

                elif not self.game and not self.pending_new_request:
                    # If game is expired
                    if event.type == pygame.KEYDOWN and event.key == pygame.K_n:
                        # Join new game
                        print('Sending join new game request')
                        asyncio.create_task(self.websocket.send(json.dumps({
                            'type': 'JOIN_GAME',
                            'session_id': self.session_id,
                        })))
                        self.pending_new_request = True

            await asyncio.sleep(interval)


async def establish_connection():
    if INSECURE and re.match('^wss', URI):
        return await websockets.connect(URI, ssl=ssl._create_unverified_context())
    else:
        return await websockets.connect(URI)


async def main():
    websocket = None
    try:
        websocket = await establish_connection()
        message_type = 'SIGN_UP' if SIGNUP else 'LOGIN'
        await websocket.send(json.dumps({'type': message_type, 'username': USERNAME, 'password': PASSWORD}))
        result = json.loads(await websocket.recv())
        if result['type'] == 'AUTHENTICATED':
            # Establish session
            session_id = result['session_id']
            user_id = result['user_id']

            print(f'User session created successfully user_id:{user_id} session_id:{session_id}')
            await websocket.send(json.dumps({'type': 'JOIN_GAME', 'session_id': session_id}))

            print('WAITING FOR ENOUGH PLAYERS TO JOIN!')

            while True:
                result = json.loads(await websocket.recv(), cls=DotsAndBoxesJSONDecoder)
                if result['type'] == 'GAME':
                    print('Starting game!')
                    # Get the game details from server
                    game_id = result['game_id']
                    game = result['game_data']
                    async with GameUI(game, websocket, session_id=session_id, game_id=game_id,
                                      user_id=user_id) as game_ui:
                        await game_ui.game_loop()
                    break
                elif result['type'] == 'SESSION_EXPIRED':
                    print('Session expired! Try logging in again')
                    break
                else:
                    # Ignore unknown messages
                    print(f"Unexpected message: {result}")

        elif result['type'] == 'UNAUTHENTICATED':
            print(f"Authentication failed. Reason: {result['error']}")
            print(f"If new user pass --signup flag, if existing user avoid it")
        else:
            print(f"Unexpected message: {result}")

    except InvalidURI:
        print("Invalid URI, please double check the URI")
    except ssl.SSLCertVerificationError:
        print("Certification verification failed. Please update your CA certificates.")
        print("run `pip3 install --upgrade certifi`")
        print("OR run the `client.py` with `--insecure` flag")
    except InvalidStatusCode:
        print("Server not found, please double check the URI")
    except socket.gaierror:
        print("Address resolution failed, please check the URI and your network connection.")
    except OSError:
        print('Make sure server is running')

    finally:
        # Game handles connection closure if game started.
        if websocket and not websocket.closed:
            await websocket.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # TODO: Close gracefully on exit
        pass
