# -*- coding: utf-8 -*-
# Copyright Duncan Macleod 2018
#
# This file is part of GWDataFind.
#
# GWDataFind is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# GWDataFind is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GWDataFind.  If not, see <http://www.gnu.org/licenses/>.

"""Pytest hooks for gwdatafind
"""

import warnings

__author__ = 'Duncan Macleod <duncan.macleod@ligo.org>'

# always present warnings during testing
warnings.simplefilter('always')
