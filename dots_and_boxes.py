from __future__ import annotations
from dataclasses import dataclass
from collections import defaultdict
from typing import List, Set, Dict, DefaultDict
import json


# Grid is used to indicate the size of dots and boxes board
# Note: 2 x 2 Board has 3 x 3 dots
@dataclass(frozen=True)
class Grid:
    rows: int
    columns: int


# Indicates a dot on the board
@dataclass(frozen=True, order=True)
class Dot:
    x: int
    y: int


# adjacent dots on grid are joined to form an edge
@dataclass(frozen=True, order=True)
class Edge:
    start: Dot
    end: Dot

    @property
    def horizontal(self):
        return self.start.x == self.end.x

    @property
    def vertical(self):
        return self.start.y == self.end.y

    @classmethod
    def new_vertical(cls, start: Dot):
        return cls(start, Dot(x=start.x + 1, y=start.y))

    @classmethod
    def new_horizontal(cls, start: Dot):
        return cls(start, Dot(x=start.x, y=start.y + 1))

    @staticmethod
    def all_edges(grid: Grid):
        """ Returns all possible edges in a given grid """
        yield from (Edge.new_horizontal(Dot(i, j)) for i in range(grid.rows + 1) for j in range(grid.columns))
        yield from (Edge.new_vertical(Dot(i, j)) for i in range(grid.rows) for j in range(grid.columns + 1))

    def adjacent_boxes(self, grid: Grid):
        """ Returns all adjacent boxes to an edge in the given grid """
        if (self.start.x == 0 and self.horizontal) or (self.start.y == 0 and self.vertical):
            return Box(self.start),
        elif (self.end.x == grid.rows and self.horizontal) or (self.end.y == grid.columns and self.vertical):
            return Box.from_end(self.end),
        else:
            return Box(self.start), Box.from_end(self.end)


@dataclass(frozen=True, order=True)
class Box:
    start: Dot

    @classmethod
    def from_end(cls, end: Dot):
        return cls(Dot(x=end.x - 1, y=end.y - 1))

    @staticmethod
    def all_boxes(grid: Grid):
        """ Returns all possible boxes in a given grid """
        yield from (Box(Dot(i, j)) for i in range(grid.rows) for j in range(grid.columns))


# Game is played by players (which are linked to users and not sessions)
# Game can continue till it expires even if individual session expires via which game was created expires
# Users can join back creating another session
@dataclass(frozen=True)
class Player:
    user_id: str
    username: str


# The Game
class DotsAndBoxes:
    def __init__(self, players: List[Player], grid=Grid(5, 5)):
        if len(players) < 2:
            raise DotsAndBoxesException('Insufficient number of players')
        # properties that cannot be reset or modified
        self._grid: Grid = grid
        self._players: List[Player] = players

        # GAME STATES
        self._turn: int = 0
        # edges not yet chosen by any player
        self._pending_edges: Set[Edge] = set(Edge.all_edges(grid))
        # map from boxes to number of pending adjacent edges
        self._pending_boxes: Dict[Box, int] = {box: 4 for box in Box.all_boxes(grid)}
        # map from player to set of chosen edges
        self._chosen_edges: DefaultDict[Player, Set[Edge]] = defaultdict(set)
        # map from player to set of won boxes
        self._won_boxes: DefaultDict[Player, Set[Box]] = defaultdict(set)
        # Last move (Useful in UI)
        self._last_move: Edge = None

    def reset(self):
        # Reset the game state to initial
        self._turn = 0
        self._pending_edges = set(Edge.all_edges(self._grid))
        self._pending_boxes = {box: 4 for box in Box.all_boxes(self._grid)}
        self._chosen_edges = defaultdict(set)
        self._won_boxes = defaultdict(set)
        self._last_move = None

    def make_move(self, player: Player, edge: Edge):
        if self.game_over:
            raise DotsAndBoxesException('Game over')
        if player != self.current_player:
            raise DotsAndBoxesException('Player cannot make the move')
        if edge not in self._pending_edges:
            raise DotsAndBoxesException('Cannot select specified edge')

        # Make the move (if all preconditions are met)
        self._pending_edges.remove(edge)
        self._chosen_edges[player].add(edge)

        update_turn = True
        for box in edge.adjacent_boxes(self._grid):
            self._pending_boxes[box] -= 1
            if self._pending_boxes[box] == 0:
                del self._pending_boxes[box]
                self._won_boxes[player].add(box)
                # Turn continues if player has won a box
                update_turn = False

        if update_turn:
            self._turn = (self._turn + 1) % len(self._players)
        self._last_move = edge

    @property
    def current_player(self):
        return self._players[self._turn]

    @property
    def players(self):
        return self._players

    @property
    def game_over(self):
        # all edges have already been chose
        return len(self._pending_edges) == 0

    @property
    def winners(self):
        # players with leading number of boxes
        # they are the winners when game is over
        winners, max_boxes = set(), 0
        for player, boxes in self._won_boxes.items():
            if len(boxes) == max_boxes:
                winners.add(player)
            elif len(boxes) > max_boxes:
                winners, max_boxes = {player}, len(boxes)
        return winners

    # Getters
    @property
    def num_players(self):
        return len(self._players)

    @property
    def grid(self):
        return self._grid

    # Helper methods for UI client
    @property
    def pending_edges(self):
        return self._pending_edges

    @property
    def last_move(self):
        return self._last_move

    @property
    def chosen_edges_to_player(self) -> Dict[Edge, Player]:
        chosen_edges = {}
        for player, edges in self._chosen_edges.items():
            for edge in edges:
                chosen_edges[edge] = player
        return chosen_edges

    @property
    def won_boxes_to_player(self) -> Dict[Box, Player]:
        won_boxes = {}
        for player, boxes in self._won_boxes.items():
            for box in boxes:
                won_boxes[box] = player
        return won_boxes

    def index(self, player):
        return self._players.index(player)

    def score(self, player):
        return len(self._won_boxes[player])

    # Properties used by JSON decoder and encoder
    # NOTE: Not to be used elsewhere
    @property
    def turn(self) -> int:
        return self._turn

    @property
    def pending_boxes(self) -> Dict[Box, int]:
        return self._pending_boxes

    @property
    def chosen_edges(self) -> DefaultDict[Player, Set[Edge]]:
        return self._chosen_edges

    @property
    def won_boxes(self) -> DefaultDict[Player, Set[Box]]:
        return self._won_boxes


class DotsAndBoxesException(Exception):
    # The exception is typically thrown when invalid move is made
    # like, move after game over, invalid edge, or out of turn (or unknown) player
    def __init__(self, message):
        super().__init__(message)
        print(f'[DotsAndBoxesException] {message}')
        self.message = message


class DotsAndBoxesJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Grid) or \
                isinstance(obj, Dot) or \
                isinstance(obj, Edge) or \
                isinstance(obj, Box) or \
                isinstance(obj, Player):
            # Dataclasses with serializable keys and public properties
            return {'__class__': obj.__class__.__name__, **obj.__dict__}
        elif isinstance(obj, DotsAndBoxes):
            # DotsAndBoxes have objects as keys, and private properties
            # So needs additional serialization effort
            return {
                '__class__': DotsAndBoxes.__name__,
                'grid': obj.grid,
                'players': obj.players,
                'turn': obj.turn,
                'last_move': obj.last_move,
                # Internal states needing transformation to make it valid JSON
                'pending_edges': list(obj.pending_edges),
                'pending_boxes': list(
                    map(lambda box: [box, obj.pending_boxes[box]], obj.pending_boxes),
                ),
                # Note: Iterating over players, as default dict is used
                'chosen_edges': list(
                    map(lambda player: [player, list(obj.chosen_edges[player])], obj.players)
                ),
                'won_boxes': list(
                    map(lambda player: [player, list(obj.won_boxes[player])], obj.players)
                ),
            }
        # Default behaviour
        return json.JSONEncoder.encode(self, obj)


class DotsAndBoxesJSONDecoder(json.JSONDecoder):
    classes = {
        Grid.__name__: Grid,
        Dot.__name__: Dot,
        Edge.__name__: Edge,
        Box.__name__: Box,
        Player.__name__: Player
    }

    def __init__(self, *args, **kwargs):
        # Documentation: object_hook, if specified, will be called with the result of every JSON object decoded and
        # its return value will be used in place of the given dict.
        json.JSONDecoder.__init__(self, object_hook=self.object_hook, *args, **kwargs)

    def object_hook(self, dct: dict):
        if '__class__' in dct:
            dct = dct.copy()  # Create a shallow copy so not to not modify the input params
            if dct['__class__'] in [
                Grid.__name__,
                Dot.__name__,
                Edge.__name__,
                Box.__name__,
                Player.__name__
            ]:
                # Handled namedtuple/dataclasses based objects
                class_name = dct.pop('__class__')
                return self.classes[class_name](**dct)
            elif dct['__class__'] == DotsAndBoxes.__name__:
                # Create a new object and update its private properties
                game = DotsAndBoxes(dct['players'], dct['grid'])
                game._turn = dct['turn']
                game._last_move = dct['last_move']
                game._pending_edges = set(dct['pending_edges'])
                game._pending_boxes = {box: count for box, count in dct['pending_boxes']}
                game._chosen_edges = defaultdict(set, {player: set(edges) for player, edges in dct['chosen_edges']})
                game._won_boxes = defaultdict(set, {player: set(boxes) for player, boxes in dct['won_boxes']})
                return game
        # Just return unknown values
        return dct
