from dots_and_boxes import *
from websockets import WebSocketClientProtocol as WSConnection

import pygame
import asyncio
import websockets
import json
import os
import argparse

parser = argparse.ArgumentParser(description='Game client')
parser.add_argument('--username', type=str, default='username', help='Username')
parser.add_argument('--password', type=str, default='password', help='Password')
parser.add_argument('--uri', type=str, default='ws://localhost:8080', help='Server WebSocket URI')
parser.add_argument("--signup", action="store_true", help="Sign up else login")
args = parser.parse_args()

URI = args.uri
USERNAME = args.username
PASSWORD = args.password
SIGNUP = bool(args.signup)  # else LOGIN

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
        color = pygame.Color((255, 255, 255))
        color.hsva = 360 * index / self.total, 100, 100, 100
        return color

    def primary(self, index):
        color = pygame.Color((255, 255, 255))
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
        self.width, self.height = 100 * game.grid.columns + 100, 100 * game.grid.rows + 150

        self.large_font = pygame.font.Font(None, 50)
        self.small_font = pygame.font.Font(None, 30)

        self.win = pygame.display.set_mode((self.width, self.height))
        pygame.display.set_caption(f'Dots and Boxes [{USERNAME}]')
        self.edges = [EdgeUI(edge) for edge in Edge.all_edges(game.grid)]
        self.box_drawer = BoxDrawer()
        self.color_util = ColorUtil(self.game.num_players)

        self.run = False

    # Context manager
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pygame.quit()

    def draw(self):
        self.win.fill(BACKGROUND)
        if self.game:
            self._draw_game()
            self._draw_status()
            if self.game.game_over:
                self._draw_text_screen_center('Game Over')
        else:
            self._draw_text_screen_center('Game Expired')

    def _draw_text_screen_center(self, message):
        text = self.large_font.render(message, True, FOREGROUND)
        text_rect = text.get_rect()
        text_rect.center = self.width // 2, self.height // 2
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
            text = self.small_font.render(f"{username}: {score}", True, self.color_util.primary(index))
            text_rect = text.get_rect()

            # Construct a new surface which will contain score, connection status, and turn indicator
            surf = pygame.Surface((text_rect.w + 50 + text_rect.h, text_rect.h + 40))
            rect = surf.get_rect()
            surf.fill(BACKGROUND)
            # Add a border to indicate turn
            if player == self.game.current_player:
                pygame.draw.rect(surf, self.color_util.secondary(index), rect, width=2)
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

    async def consume_messages(self):
        while self.run:
            async for message in self.websocket:
                response = json.loads(message)
                # TODO: Handle all response types
                if response['type'] == 'GAME':
                    self.game_id = response['game_id']
                    self.game: DotsAndBoxes = DotsAndBoxes.decode(response['game_data'])
                    statuses = response['player_status']
                    for player in self.game.players:
                        self.connection_status[player] = statuses[self.game.index(player)]
                elif response['type'] == 'GAME_EXPIRED':
                    # Game expired
                    self.game = None
                else:
                    print(response)

    async def game_loop(self, interval=0.05):
        self.run = True

        # Updates based on server messages
        asyncio.create_task(self.consume_messages())

        while self.run:

            # Render the UI
            self.draw()
            pygame.display.update()

            for event in pygame.event.get():
                if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_q):
                    if self.game and not self.game.game_over:
                        # If game is still running
                        # Send exit message to server
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
                                        # Make the move locally
                                        self.game.make_move(self.game.current_player, edge.edge)
                                        # Send the move to server
                                        asyncio.create_task(self.websocket.send(json.dumps({
                                            'type': 'MAKE_MOVE',
                                            'session_id': self.session_id,
                                            'game_id': self.game_id,
                                            'edge_data': edge.edge.encode(),
                                        })))

            await asyncio.sleep(interval)


async def main():
    websocket = None
    try:
        websocket = await websockets.connect(URI)
        message_type = 'SIGN_UP' if SIGNUP else 'LOGIN'
        await websocket.send(json.dumps({'type': message_type, 'username': USERNAME, 'password': PASSWORD}))
        result = json.loads(await websocket.recv())
        if result['type'] == 'AUTHENTICATED':
            session_id = result['session_id']
            user_id = result['user_id']

            await websocket.send(json.dumps({'type': 'JOIN_GAME', 'session_id': session_id}))
            result = json.loads(await websocket.recv())

            if result['type'] == 'GAME':
                game_id = result['game_id']
                game = DotsAndBoxes.decode(result['game_data'])
                with GameUI(game, websocket, session_id=session_id, game_id=game_id, user_id=user_id) as game_ui:
                    await game_ui.game_loop()
            else:
                print(result)
        else:
            print(result)

    finally:
        if websocket and not websocket.closed:
            await websocket.close()


if __name__ == '__main__':
    asyncio.run(main())
