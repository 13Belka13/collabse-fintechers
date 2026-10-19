// SPDX-License-Identifier: MIT
pragma solidity 0.8.31;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC3009} from "@openzeppelin/contracts/token/ERC20/extensions/draft-ERC3009.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/// @title tCZK — the bank's tokenized deposit (SPEC §3).
/// 1 tCZK = 1 Kč of a client's balance at the bank, 2 decimals (haléře). Only bank clients and
/// approved escrow infrastructure may hold it. Strict mode: ordinary transfers only go through
/// contracts the bank trusts (its x402 relayer, the escrow collector and token store), so the
/// bank's checks cannot be bypassed by calling the token directly.
/// EIP-3009 with both (v,r,s) and `bytes signature` overloads; the escrow collector calls only
/// receiveWithAuthorization(..., bytes) (selector 0x88b7ab63).
contract TCZK is ERC3009, AccessControl {
    bytes32 public constant ISSUER_ROLE = keccak256("ISSUER_ROLE");
    bytes32 public constant COMPLIANCE_ROLE = keccak256("COMPLIANCE_ROLE");
    bytes32 public constant ENFORCER_ROLE = keccak256("ENFORCER_ROLE");
    bytes32 public constant ROUTER_ROLE = keccak256("ROUTER_ROLE");

    mapping(address => bool) public allowed;
    mapping(address => bool) public frozen;
    /// keccak256(salt ‖ account number) — never the number or IBAN itself.
    mapping(address => bytes32) public accountRef;
    bool public strictMode = true;

    error NotAllowlisted(address account);
    error AccountFrozen(address account);
    error ZeroAddress();
    error TransferViaBankOnly(address caller);

    event Allowlisted(address indexed account, bytes32 indexed ref);
    event Delisted(address indexed account);
    event Frozen(address indexed account); // no reason on chain: the client must not be tipped off (§6.7)
    event Unfrozen(address indexed account);
    event Deposit(address indexed to, uint256 amount, bytes32 indexed bankTxRef);
    event Withdrawal(address indexed from, uint256 amount, bytes32 indexed bankTxRef);
    event ForcedTransfer(address indexed from, address indexed to, uint256 amount, bytes32 caseRef);
    event StrictModeSet(bool on);

    constructor(address admin) ERC20("tCZK Deposit Token", "tCZK") EIP712("tCZK Deposit Token", "1") {
        if (admin == address(0)) revert ZeroAddress();
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
    }

    // ---- views ----

    function decimals() public pure override returns (uint8) {
        return 2;
    }

    function version() external pure returns (string memory) {
        return "1";
    }

    // solhint-disable-next-line func-name-mixedcase
    function DOMAIN_SEPARATOR() external view returns (bytes32) {
        return _domainSeparatorV4();
    }

    function isAllowed(address a) external view returns (bool) {
        return allowed[a];
    }

    function isFrozen(address a) external view returns (bool) {
        return frozen[a];
    }

    function accountRefOf(address a) external view returns (bytes32) {
        return accountRef[a];
    }

    // ---- compliance ----

    function allow(address a, bytes32 ref) external onlyRole(COMPLIANCE_ROLE) {
        if (a == address(0)) revert ZeroAddress();
        allowed[a] = true;
        accountRef[a] = ref;
        emit Allowlisted(a, ref);
    }

    function disallow(address a) external onlyRole(COMPLIANCE_ROLE) {
        allowed[a] = false;
        delete accountRef[a];
        emit Delisted(a);
    }

    function freeze(address a) external onlyRole(COMPLIANCE_ROLE) {
        frozen[a] = true;
        emit Frozen(a);
    }

    function unfreeze(address a) external onlyRole(COMPLIANCE_ROLE) {
        frozen[a] = false;
        emit Unfrozen(a);
    }

    function setStrictMode(bool on) external onlyRole(DEFAULT_ADMIN_ROLE) {
        strictMode = on;
        emit StrictModeSet(on);
    }

    // ---- issuance ----

    /// Client deposited Kč at the bank → the same amount appears as tCZK in its wallet.
    function mint(address to, uint256 amount, bytes32 bankTxRef) external onlyRole(ISSUER_ROLE) {
        _mint(to, amount);
        emit Deposit(to, amount, bankTxRef);
    }

    /// Withdrawal back to the account. Works for delisted holders too (burn is never checked).
    function burn(address from, uint256 amount, bytes32 bankTxRef) external onlyRole(ISSUER_ROLE) {
        _burn(from, amount);
        emit Withdrawal(from, amount, bankTxRef);
    }

    /// Court order / recovery: moves funds regardless of the sender's state; the receiver must be a
    /// client in good standing.
    function forcedTransfer(address from, address to, uint256 amount, bytes32 caseRef)
        external
        onlyRole(ENFORCER_ROLE)
    {
        if (from == address(0) || to == address(0)) revert ZeroAddress();
        _check(to);
        ERC20._update(from, to, amount);
        emit ForcedTransfer(from, to, amount, caseRef);
    }

    // ---- EIP-3009 `bytes signature` overloads (USDC v2 / escrow collector ABI) ----

    function transferWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        bytes memory signature
    ) public {
        bytes32 hash = _hashTypedDataV4(
            keccak256(abi.encode(TRANSFER_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce))
        );
        require(SignatureChecker.isValidSignatureNow(from, hash, signature), ERC3009InvalidSignature());
        _transferWithAuthorization(from, to, value, validAfter, validBefore, nonce);
    }

    function receiveWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        bytes memory signature
    ) public {
        bytes32 hash = _hashTypedDataV4(
            keccak256(abi.encode(RECEIVE_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce))
        );
        require(SignatureChecker.isValidSignatureNow(from, hash, signature), ERC3009InvalidSignature());
        require(to == _msgSender(), ERC20InvalidReceiver(to));
        _transferWithAuthorization(from, to, value, validAfter, validBefore, nonce);
    }

    function cancelAuthorization(address authorizer, bytes32 nonce, bytes memory signature) public {
        bytes32 hash = _hashTypedDataV4(keccak256(abi.encode(CANCEL_AUTHORIZATION_TYPEHASH, authorizer, nonce)));
        require(SignatureChecker.isValidSignatureNow(authorizer, hash, signature), ERC3009InvalidSignature());
        _cancelAuthorization(authorizer, nonce);
    }

    // ---- transfer rules (§3.2, §3.4) ----

    function _update(address from, address to, uint256 value) internal override {
        if (to != address(0)) {
            if (from != address(0)) {
                if (strictMode && !hasRole(ROUTER_ROLE, _msgSender())) revert TransferViaBankOnly(_msgSender());
                _check(from);
            }
            _check(to);
        }
        super._update(from, to, value);
    }

    function _check(address a) internal view {
        if (!allowed[a]) revert NotAllowlisted(a);
        if (frozen[a]) revert AccountFrozen(a);
    }
}
