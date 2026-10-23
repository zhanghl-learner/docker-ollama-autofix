#include "calculator.hpp"

#include <iostream>

int main() {
    const int actual = add(2, 3);
    if (actual != 5) {
        std::cerr << "Expected add(2, 3) to equal 5, received " << actual << '\n';
        return 1;
    }
    std::cout << "C++ test passed\n";
    return 0;
}
