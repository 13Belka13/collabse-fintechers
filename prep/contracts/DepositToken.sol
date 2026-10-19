// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// Demo tokenized deposit: 1 token = 1 unit of a client's bank balance.
/// Only the bank mints/burns; only allowlisted holders (bank clients and approved
/// infrastructure such as an escrow) may send or receive. EIP-3009 transfers make it
/// payable through x402 exactly like USDC/EURC.
contract DepositToken {
    string public name;
    string public constant symbol = "tCZK";
    string public constant version = "1";
    uint8 public constant decimals = 2;
    address public immutable bank;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    mapping(address => bool) public allowlisted;
    mapping(address => mapping(bytes32 => bool)) public authorizationState;

    bytes32 public immutable DOMAIN_SEPARATOR;
    bytes32 public constant TRANSFER_WITH_AUTHORIZATION_TYPEHASH = keccak256(
        "TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)");
    bytes32 public constant RECEIVE_WITH_AUTHORIZATION_TYPEHASH = keccak256(
        "ReceiveWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)");

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    event AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce);
    event Allowlisted(address indexed account, bool allowed);

    error NotBank();
    error NotAllowlisted(address account);
    error AuthorizationInvalid();

    constructor(string memory name_) {
        name = name_;
        bank = msg.sender;
        DOMAIN_SEPARATOR = keccak256(abi.encode(
            keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
            keccak256(bytes(name_)), keccak256(bytes(version)), block.chainid, address(this)));
    }

    modifier onlyBank() { if (msg.sender != bank) revert NotBank(); _; }

    function setAllowlisted(address account, bool allowed) external onlyBank {
        allowlisted[account] = allowed;
        emit Allowlisted(account, allowed);
    }

    function mint(address to, uint256 value) external onlyBank {
        if (!allowlisted[to]) revert NotAllowlisted(to);
        totalSupply += value;
        balanceOf[to] += value;
        emit Transfer(address(0), to, value);
    }

    function burn(address from, uint256 value) external onlyBank {
        balanceOf[from] -= value;
        totalSupply -= value;
        emit Transfer(from, address(0), value);
    }

    function transfer(address to, uint256 value) external returns (bool) {
        _transfer(msg.sender, to, value);
        return true;
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        allowance[from][msg.sender] -= value;
        _transfer(from, to, value);
        return true;
    }

    // EIP-3009, (v, r, s) and bytes-signature overloads like USDC v2
    function transferWithAuthorization(address from, address to, uint256 value, uint256 validAfter,
        uint256 validBefore, bytes32 nonce, uint8 v, bytes32 r, bytes32 s) public {
        _useAuthorization(TRANSFER_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce, v, r, s);
        _transfer(from, to, value);
    }

    function transferWithAuthorization(address from, address to, uint256 value, uint256 validAfter,
        uint256 validBefore, bytes32 nonce, bytes calldata signature) external {
        (uint8 v, bytes32 r, bytes32 s) = _split(signature);
        transferWithAuthorization(from, to, value, validAfter, validBefore, nonce, v, r, s);
    }

    function receiveWithAuthorization(address from, address to, uint256 value, uint256 validAfter,
        uint256 validBefore, bytes32 nonce, uint8 v, bytes32 r, bytes32 s) public {
        if (to != msg.sender) revert AuthorizationInvalid();
        _useAuthorization(RECEIVE_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce, v, r, s);
        _transfer(from, to, value);
    }

    function receiveWithAuthorization(address from, address to, uint256 value, uint256 validAfter,
        uint256 validBefore, bytes32 nonce, bytes calldata signature) external {
        (uint8 v, bytes32 r, bytes32 s) = _split(signature);
        receiveWithAuthorization(from, to, value, validAfter, validBefore, nonce, v, r, s);
    }

    function _useAuthorization(bytes32 typehash, address from, address to, uint256 value, uint256 validAfter,
        uint256 validBefore, bytes32 nonce, uint8 v, bytes32 r, bytes32 s) internal {
        if (block.timestamp <= validAfter || block.timestamp >= validBefore) revert AuthorizationInvalid();
        if (authorizationState[from][nonce]) revert AuthorizationInvalid();
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", DOMAIN_SEPARATOR,
            keccak256(abi.encode(typehash, from, to, value, validAfter, validBefore, nonce))));
        address signer = ecrecover(digest, v, r, s);
        if (signer == address(0) || signer != from) revert AuthorizationInvalid();
        authorizationState[from][nonce] = true;
        emit AuthorizationUsed(from, nonce);
    }

    function _transfer(address from, address to, uint256 value) internal {
        if (!allowlisted[from]) revert NotAllowlisted(from);
        if (!allowlisted[to]) revert NotAllowlisted(to);
        balanceOf[from] -= value;
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }

    function _split(bytes calldata sig) internal pure returns (uint8 v, bytes32 r, bytes32 s) {
        if (sig.length != 65) revert AuthorizationInvalid();
        r = bytes32(sig[0:32]);
        s = bytes32(sig[32:64]);
        v = uint8(sig[64]);
    }
}
