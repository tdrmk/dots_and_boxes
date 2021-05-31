import pickle


# Deprecating in favour of JSON based serialization
# for cross-platform compatibility.
class HexPickleSerializer:
    # Helps in serializing (encoding/decoding) objects
    # to and from hex using pickle.
    def encode(self) -> str:
        return bytes.hex(pickle.dumps(self))

    @classmethod
    def decode(cls, data: str):
        return pickle.loads(bytes.fromhex(data))
