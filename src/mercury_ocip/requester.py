class BaseRequester(ABC):
    """Base class for all requesters.

    Args:
        logger (logging.Logger): The logger of the requester.
        host (str): The host of the server.
        port (int): The port of the server.
        timeout (int): The timeout of the requester.
        session_id (str): The session id of the requester.
    """

    def __init__(
        self,
        logger: logging.Logger,
        host: str,
        port: int,
        timeout: int,
        session_id: str,
    ) -> None:
        self.logger = logger
        self.host = host
        self.port = port
        self.timeout = timeout
        self.session_id = session_id

    @abstractmethod
    def send_request(
        self, command: str
    ) -> Union[RequestResult, Awaitable[RequestResult]]:
        """Sends a request to the server.

        Args:
            command (BroadworksCommand): The command to send to the server.
        """
        pass

    @abstractmethod
    def connect(
        self,
    ) -> Union[ConnectResult, Awaitable[ConnectResult]]:
        """Connects to the server.

        Returns:
            None if successful, or a tuple of (ExceptionType, Exception) if an error occurs.
            For async implementations, returns an awaitable of the same.
        """
        pass

    @abstractmethod
    def disconnect(self) -> Union[DisconnectResult, Awaitable[DisconnectResult]]:
        """Disconnects from the server."""
        pass

    def build_oci_xml(self, command: str) -> bytes:
        """Builds an OCI XML request from the given BroadworksCommand.

        Constructs an XML document with a session ID and the encoded command,
        wrapped in a BroadsoftDocument element with the OCI protocol.

        Args:
            command (BroadworksCommand): The command to be encoded into the XML.

        Returns:
            bytes: The serialized XML document as bytes, encoded with ISO-8859-1.
        """

        ElementMaker = builder.ElementMaker(
            namespace="C",
            nsmap={None: "C", "xsi": "http://www.w3.org/2001/XMLSchema-instance"},
        )

        session_id = etree.Element("sessionId")
        session_id.text = self.session_id
        session_id.set("xmlns", "")

        command_element = etree.fromstring(command.encode("ISO-8859-1"))

        broadsoft_doc = ElementMaker.BroadsoftDocument(
            session_id, command_element, protocol="OCI"
        )

        return etree.tostring(
            broadsoft_doc, xml_declaration=True, encoding="ISO-8859-1"
        )

    def __del__(self) -> None:
        self.disconnect()


class AsyncTCPRequester(BaseRequester):
    """An asynchronous TCP requester for BroadWorks OCI-P.

    This class manages an asynchronous connection to a BroadWorks Application
    Server. It will open a TCP Socket connection, using 2209 for an SSL wrapped
    socket for encrypted traffic.

    Args:
        session_id (str): The session ID passed to keep the session alive.
        logger (logging.Logger): An instance of `logging.Logger` for logging messages.
        host (str): The hostname or IP address of the BroadWorks server.
        port (int): The port for the OCI-P interface, defaults to 2209.
        timeout (int): The timeout for HTTP requests in seconds, defaults to 10.
    """

    def __init__(
        self,
        logger: logging.Logger,
        host: str,
        port: int = 2209,
        timeout: int = 10,
        session_id: str = "",
        tls: bool = True,
    ) -> None:
        self.reader: Optional[StreamReader] = None
        self.writer: Optional[StreamWriter] = None
        self.tls = tls
        super().__init__(
            logger=logger,
            host=host,
            port=port,
            timeout=timeout,
            session_id=session_id,
        )

    async def connect(self) -> ConnectResult:
        """Connects to the server."""
        if self.reader is None and self.writer is None:
            try:
                if self.tls:
                    context: ssl.SSLContext = ssl.create_default_context()
                    self.reader, self.writer = await asyncio.wait_for(
                        asyncio.open_connection(
                            host=self.host, port=self.port, ssl=context
                        ),
                        timeout=self.timeout,
                    )
                    self.logger.info(
                        f"Initiated socket on {self.__class__.__name__}: {self.host}:{self.port}"
                    )
                else:
                    self.reader, self.writer = await asyncio.wait_for(
                        asyncio.open_connection(self.host, self.port),
                        timeout=self.timeout,
                    )
            except Exception as e:
                self.logger.error(
                    f"Failed to initiate socket on {self.__class__.__name__}: {e}"
                )
                return MErrorSocketInitialisation(str(e))

    async def disconnect(self) -> DisconnectResult:
        """Disconnects from the server."""
        if self.reader and self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception as e:
                self.logger.warning(
                    f"Exception: {e} was raised when attemping to close {self.__class__.__name__}, but was ignored."
                )
                pass
            finally:
                self.writer = None
                self.reader = None

    async def send_request(self, command: str) -> RequestResult:
        """Sends a request to the server.

        Args:
            command (BroadworksCommand): The command to send to the server.

        Returns:
            Any: The response from the server.
        """
        try:
            if self.reader is None or self.writer is None:
                result: MError | None = await self.connect()
                if isinstance(result, MError):  # Error returned
                    return result

            assert self.reader is not None and self.writer is not None

            command_bytes: bytes = self.build_oci_xml(command)

            self.logger.debug(f"Sending command to {self.host}:{self.port}: {command}")

            self.writer.write(command_bytes + b"\n")
            await self.writer.drain()

            content = b""
            while True:
                try:
                    chunk: bytes = await asyncio.wait_for(
                        self.reader.read(4096), timeout=self.timeout
                    )
                except asyncio.TimeoutError as e:
                    self.logger.error(
                        f"Socket read timed out in {self.__class__.__name__}: {e}"
                    )
                    return MErrorSocketTimeout(str(e))

                if not chunk:
                    break

                content += chunk
                if b"</BroadsoftDocument>" in content:
                    break
            return content.rstrip(b"\n").decode("ISO-8859-1")

        except Exception as e:
            self.logger.error(
                f"Failed to send command over {self.__class__.__name__}: {e}"
            )
            return MErrorSendRequestFailed(str(e))


def create_requester(
    logger: logging.Logger,
    session_id: str,
    host: str,
    port: int,
    conn_type: str = "SOAP",
    async_: bool = True,
    timeout: int = 10,
    tls: bool = True,
) -> BaseRequester:
    """Factory function to create a requester.

    Args:
        logger (logging.Logger): The logger to use.
        session_id (str): The session ID to use.
        host (str): The host to connect to.
        port (int): The port to connect to.
        conn_type (str): The connection type to use.
        async_ (bool): Whether to use an asynchronous requester.
        timeout (int): The timeout to use.

    Returns:
        BaseRequester: The created requester.
    """
    if conn_type == "SOAP":
        if async_:
            return AsyncSOAPRequester(
                host=host,
                port=port,
                timeout=timeout,
                logger=logger,
                session_id=session_id,
            )
        else:
            return SyncSOAPRequester(
                host=host,
                port=port,
                timeout=timeout,
                logger=logger,
                session_id=session_id,
            )
    elif conn_type == "TCP":
        if async_:
            return AsyncTCPRequester(
                host=host,
                port=port,
                timeout=timeout,
                logger=logger,
                session_id=session_id,
                tls=tls,
            )
        else:
            return SyncTCPRequester(
                host=host,
                port=port,
                timeout=timeout,
                logger=logger,
                session_id=session_id,
                tls=tls,
            )
    else:
        raise ValueError(f"Unknown connection type: {conn_type}")
